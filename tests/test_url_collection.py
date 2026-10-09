import hashlib
import io
import json
import zipfile

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import create_app
from app.products import Product, ProductAsset
from app.taobao.parser import TaobaoParser
from app.taobao.session import ManualRequired
from app.tasks import TaskStatus


URL = "https://detail.tmall.com/item.htm?id=123456789"
HTML = '''<script type="application/ld+json">{"@type":"Product","name":"测试杯","image":["https://img.alicdn.com/main.png"],"offers":{"price":"29.90"}}</script><div id="J_DivItemDesc"><img src="https://img.alicdn.com/a.png"><img src="https://img.alicdn.com/b.png"></div>'''


class FakeCollector:
    class Session:
        def cookies(self):
            return {}

    session = Session()

    def collect_product(self, task_id, url):
        return TaobaoParser().parse_product(HTML, url)

    def close(self):
        pass


class ChallengedCollector(FakeCollector):
    def __init__(self):
        self.ready = False

    def collect_product(self, task_id, url):
        if not self.ready:
            raise ManualRequired("请在 Chromium 手工完成登录")
        return super().collect_product(task_id, url)


class BrokenCollector(FakeCollector):
    def collect_product(self, task_id, url):
        raise TimeoutError("页面打开超时")


class VideoCollector(FakeCollector):
    def collect_product(self, task_id, url):
        parsed = super().collect_product(task_id, url)
        parsed.videos.append("https://cloud.video.taobao.com/example.mp4")
        return parsed


class DownloadStore:
    def __init__(self, real):
        self.real = real
        self.data_dir = real.data_dir
        self.fail_b = True
        self.calls = []

    def download_original(self, product_id, source_url, role, **kwargs):
        self.calls.append(source_url)
        if source_url.endswith("b.png") and self.fail_b:
            raise RuntimeError("HTTP 403")
        stream = io.BytesIO()
        Image.new("RGB", (20, 10), "blue" if source_url.endswith("b.png") else "red").save(stream, format="PNG")
        return self.real.save_original_bytes(product_id, source_url, role, "image/png", stream.getvalue())



class VideoFailStore(DownloadStore):
    def download_original(self, product_id, source_url, role, **kwargs):
        if role == "video":
            raise RuntimeError("HTTP 404")
        return super().download_original(product_id, source_url, role, **kwargs)


class SameBytesStore(DownloadStore):
    def download_original(self, product_id, source_url, role, **kwargs):
        stream = io.BytesIO()
        Image.new("RGB", (20, 10), "red").save(stream, format="PNG")
        return self.real.save_original_bytes(product_id, source_url, role, "image/png", stream.getvalue())


def test_url_collection_partial_retry_and_export(tmp_path):
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    with TestClient(app) as client:
        runner = app.state.collector_runner
        runner.collector = FakeCollector()
        store = DownloadStore(runner.asset_store)
        runner.asset_store = store
        invalid = client.post("/api/collection/jobs", json={"url": "https://evil.example/item.htm?id=1"})
        assert invalid.status_code == 422
        assert client.get("/collection").status_code == 200
        created = client.post("/api/collection/jobs", json={"url": URL})
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        assert app.state.worker.run_once()
        detail = client.get(f"/api/collection/jobs/{task_id}").json()
        assert detail["task"]["status"] == TaskStatus.PARTIAL
        assert detail["result"]["stage"] == "部分完成"
        assert len(detail["result"]["asset_errors"]) == 1
        product_id = detail["result"]["product_id"]
        with app.state.session_factory() as session:
            assert session.scalar(select(Product).where(Product.local_product_id == product_id))
            originals = session.scalars(select(ProductAsset).where(ProductAsset.product_id == product_id)).all()
            assert len(originals) == 1
            assert originals[0].original_asset_id is None
            original_bytes = (tmp_path / "data" / originals[0].relative_path).read_bytes()
            assert originals[0].sha256 == hashlib.sha256(original_bytes).hexdigest()
        data_file = tmp_path / "data" / "products" / product_id / "source" / "product.json"
        manifest = json.loads(data_file.read_text(encoding="utf-8"))
        assert client.get(f"/api/collection/jobs/{task_id}/product.json").json()["source_product_id"] == "123456789"
        assert manifest["source_product_id"] == "123456789"
        assert len(manifest["images"]) == 3
        assert manifest["images"][0]["asset_id"]
        assert manifest["images"][2]["error"] == "HTTP 403"
        response = client.get(f"/api/collection/jobs/{task_id}/export.zip")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert "product.json" in archive.namelist()
            assert len([name for name in archive.namelist() if name.startswith("assets/")]) == 1
        store.fail_b = False
        assert client.post(f"/api/collection/jobs/{task_id}/retry").status_code == 200
        assert app.state.worker.run_once()
        detail = client.get(f"/api/collection/jobs/{task_id}").json()
        assert detail["task"]["status"] == TaskStatus.SUCCESS
        assert detail["result"]["saved_originals"] == 1
        assert store.calls.count("https://img.alicdn.com/a.png") == 1
        with app.state.session_factory() as session:
            assert len(session.scalars(select(Product).where(Product.source_product_id == "123456789")).all()) == 1


def test_product_waits_for_manual_login_and_can_resume(tmp_path):
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    with TestClient(app) as client:
        collector = ChallengedCollector()
        app.state.collector_runner.collector = collector
        app.state.collector_runner.asset_store = DownloadStore(app.state.collector_runner.asset_store)
        job_id = client.post("/api/collection/jobs", json={"url": URL}).json()["task_id"]
        assert app.state.worker.run_once()
        detail = client.get(f"/api/collection/jobs/{job_id}").json()
        assert detail["task"]["status"] == TaskStatus.NEEDS_MANUAL
        assert "手工完成登录" in detail["manual_reason"]
        collector.ready = True
        assert client.post(f"/api/collection/jobs/{job_id}/resume").status_code == 200
        assert app.state.worker.run_once()
        assert client.get(f"/api/collection/jobs/{job_id}").json()["task"]["status"] == TaskStatus.PARTIAL


def test_page_open_failure_does_not_create_product(tmp_path):
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    with TestClient(app) as client:
        app.state.collector_runner.collector = BrokenCollector()
        job_id = client.post("/api/collection/jobs", json={"url": URL}).json()["task_id"]
        assert app.state.worker.run_once()
        detail = client.get(f"/api/collection/jobs/{job_id}").json()
        assert detail["task"]["status"] == TaskStatus.FAILED
        assert "页面打开超时" in detail["task"]["error_message"]
        with app.state.session_factory() as session:
            assert not session.scalars(select(Product)).all()


def test_collection_excludes_video(tmp_path):
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    with TestClient(app) as client:
        runner = app.state.collector_runner
        runner.collector = VideoCollector()
        store = VideoFailStore(runner.asset_store)
        store.fail_b = False
        runner.asset_store = store
        job_id = client.post("/api/collection/jobs", json={"url": URL}).json()["task_id"]
        assert app.state.worker.run_once()
        detail = client.get(f"/api/collection/jobs/{job_id}").json()
        assert detail["task"]["status"] == TaskStatus.SUCCESS
        assert detail["result"]["video_count"] == 0
        assert detail["result"]["main_count"] == 1
        assert detail["result"]["detail_count"] == 1
        assert detail["result"]["asset_errors"] == []
        assert store.calls == ["https://img.alicdn.com/main.png", "https://img.alicdn.com/a.png", "https://img.alicdn.com/b.png"]


def test_same_content_under_two_urls_reuses_one_original(tmp_path):
    app = create_app(data_dir=tmp_path / "data", start_worker=False)
    with TestClient(app) as client:
        runner = app.state.collector_runner
        runner.collector = FakeCollector()
        runner.asset_store = SameBytesStore(runner.asset_store)
        job_id = client.post("/api/collection/jobs", json={"url": URL}).json()["task_id"]
        assert app.state.worker.run_once()
        detail = client.get(f"/api/collection/jobs/{job_id}").json()
        assert detail["task"]["status"] == TaskStatus.SUCCESS
        product_id = detail["result"]["product_id"]
        with app.state.session_factory() as session:
            originals = session.scalars(select(ProductAsset).where(ProductAsset.product_id == product_id)).all()
            assert len(originals) == 1
        manifest = client.get(f"/api/collection/jobs/{job_id}/product.json").json()
        assert len(manifest['media']) == 3
        assert all(m['file_included'] is False for m in manifest['media'])
        response = client.get(f'/api/collection/jobs/{job_id}/export.zip')
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            packaged = json.loads(archive.read('product.json'))
            assert len({m['relative_path'] for m in packaged['media']}) == 1
            assert len([n for n in archive.namelist() if n.startswith('assets/')]) == 1
