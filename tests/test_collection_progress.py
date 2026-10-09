"""Progress shown to users comes from actual collection state and saved names."""

import httpx
from fastapi.testclient import TestClient

from app.collection_service import enqueue_collection
from app.config import ScraperAPISettings
from app.main import create_app
from app.product_service import save_collected
from app.products import CollectionJob
from app.scraperapi_service import CollectionFetchService, ScraperAPIService
from app.taobao.collector import TaobaoCollector
from app.taobao.parser import ParsedProduct, TaobaoParser
from app.tasks import Task, TaskStatus


SHOP = "https://bolisidg.tmall.com/category.htm"
PRODUCT = "https://detail.tmall.com/item.htm?id=835621215792"


def test_shop_name_comes_from_shop_metadata_and_title():
    parser = TaobaoParser()
    html = '''<title>首页-博力斯电工旗舰店-天猫Tmall.com</title>
        <script>window.g_config = {"seller":{"shopName":"博力斯电工旗舰店"}};</script>'''
    assert parser.parse_shop_name(html) == "博力斯电工旗舰店"
    assert parser.parse_shop_name("<title>首页-博力斯电工旗舰店-天猫Tmall.com</title>") == "博力斯电工旗舰店"
    assert parser.parse_shop_name("<title>天猫Tmall.com</title>") is None


def test_shop_progress_reads_saved_shop_name_and_legacy_product_title(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        with app.state.session_factory.begin() as session:
            parent = enqueue_collection(session, url=SHOP, kind="shop")
            other_page = enqueue_collection(session, url=SHOP + "?sort=desc", kind="shop")
            child = enqueue_collection(session, url=PRODUCT, kind="product", child=True)
            parent_id, other_id, child_id = parent.task_id, other_page.task_id, child.task_id
            session.get(Task, parent_id).status = TaskStatus.PENDING
            session.get(Task, child_id).status = TaskStatus.RUNNING
            session.get(CollectionJob, parent_id).result_data = {
                "child_task_ids": [child_id], "discovery_complete": True,
                "stage": "正在逐商品采集下载",
            }
            session.get(CollectionJob, child_id).result_data = {
                "child_only": True, "parent_task_ids": [parent_id],
                "stage": "正在下载原图 3/8", "stage_at": "2026-09-29T10:00:00Z",
            }
            save_collected(session, ParsedProduct(
                source_product_id="835621215792", source_url=PRODUCT,
                title="JG电缆压线耳接头铜鼻子", raw_data={"title": "JG电缆压线耳接头铜鼻子"},
            ), child_id)
        folder = tmp_path / "playwright" / "pages"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{parent_id}-1.html").write_text(
            "<title>首页-博力斯电工旗舰店-天猫Tmall.com</title>", encoding="utf-8")

        result = client.get(f"/api/collection/jobs/{parent_id}").json()["result"]
        assert result["shop_name"] == "博力斯电工旗舰店"
        assert result["products"][0]["title"] == "JG电缆压线耳接头铜鼻子"
        assert result["products"][0]["stage"] == "正在下载原图 3/8"
        assert result["products"][0]["stage_at"] == "2026-09-29T10:00:00Z"
        assert client.get(f"/api/collection/jobs/{other_id}").json()["result"]["shop_name"] == "博力斯电工旗舰店"


def test_public_product_request_reports_its_waiting_step():
    stages = []
    service = CollectionFetchService(ScraperAPIService(
        ScraperAPISettings(api_key="test-key", enabled=True, max_attempts=1),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<h1>商品</h1>"))))
    service.stage = stages.append
    html, _, _ = service.fetch(PRODUCT, lambda: (_ for _ in ()).throw(AssertionError("unexpected browser")))
    assert html == "<h1>商品</h1>"
    assert stages == ["正在通过 ScraperAPI 请求商品页面"]


def test_stage_timestamp_changes_when_step_changes(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        task_id = client.post("/api/collection/jobs", json={"url": PRODUCT}).json()["task_id"]
        app.state.collector_runner._stage(task_id, "正在解析商品资料")
        result = client.get(f"/api/collection/jobs/{task_id}").json()["result"]
        assert result["stage"] == "正在解析商品资料"
        assert result["stage_at"].endswith("Z")


def test_public_collection_reports_parsing_and_saves_discovered_shop_name(tmp_path):
    def response(request):
        target = request.url.params["url"]
        if "item.htm" in target:
            return httpx.Response(200, text="<h1>JG电缆压线耳接头铜鼻子</h1>")
        return httpx.Response(200, text=(
            '<title>首页-博力斯电工旗舰店-天猫Tmall.com</title>'
            f'<a href="{PRODUCT}">商品</a>'))

    stages, progress = [], []
    collector = TaobaoCollector(tmp_path, fetcher=CollectionFetchService(ScraperAPIService(
        ScraperAPISettings(api_key="test-key", enabled=True, max_attempts=1),
        transport=httpx.MockTransport(response))))
    collector.bind_task(lambda: None, stages.append)
    collector.collect_product("product", PRODUCT)
    assert stages == ["正在通过 ScraperAPI 请求商品页面", "正在解析商品资料"]
    stages.clear()
    assert collector.discover_shop("shop", SHOP,
        on_progress=lambda urls: progress.append((urls, collector.shop_name))) == [PRODUCT]
    assert progress == [([PRODUCT], "博力斯电工旗舰店")]
    assert stages == ["正在通过 ScraperAPI 请求店铺商品列表", "正在解析店铺商品列表"]
