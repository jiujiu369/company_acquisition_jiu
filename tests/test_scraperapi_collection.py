"""Exercise the real collector, parser, worker and browser around public fallback."""

import io
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import create_app
from app.products import CollectionJob, Product, ProductAsset
from app.taobao.collector import TaobaoCollector
from app.taobao.parser import TaobaoParser
from app.taobao.session import ManualRequired, TaobaoSession
from app.tasks import Task, TaskStatus, utc_now


PRODUCT = "https://item.taobao.com/item.htm?id=123"
SHOP = "https://shop123.taobao.com/"
DETAIL = '<h1>真实采集样例</h1><div id="J_DivItemDesc"><img src="https://img.alicdn.com/detail.png"><div class="recommend"><img src="https://img.alicdn.com/ad.jpg"></div></div><img src="https://img.alicdn.com/logo.jpg">'


def fetcher(handler, enabled=True):
    from app.config import ScraperAPISettings
    from app.scraperapi_service import CollectionFetchService, ScraperAPIService
    return CollectionFetchService(ScraperAPIService(
        ScraperAPISettings(api_key="test-credential", enabled=enabled),
        transport=httpx.MockTransport(handler), sleep=lambda _: None))


class NoBrowserSession:
    def open(self, *args):
        pytest.fail("ScraperAPI HTML must not start a login browser")

    def load_product_details(self):
        pytest.fail("Static HTML requires no second browser")

    def cookies(self):
        pytest.fail("Public collection must not read login cookies")

    def iter_shop_pages(self, *args):
        pytest.fail("Public shop acquisition must not use Playwright")

    def close(self):
        pass


def test_static_public_product_reuses_filters_without_browser_or_cookies(tmp_path):
    collector = TaobaoCollector(tmp_path, session=NoBrowserSession(),
                                fetcher=fetcher(lambda request: httpx.Response(200, text=DETAIL)))
    product = collector.collect_product("static", PRODUCT)
    assert product.title == "真实采集样例"
    assert [image["url"] for image in product.images] == ["https://img.alicdn.com/detail.png"]
    assert collector.cookies() == {}
    assert len(list((tmp_path / "playwright/pages").glob("static-*.html"))) == 1


def test_shop_static_catalog_and_pagination_share_service_and_deduplicate(tmp_path):
    targets = []
    def handler(request):
        target = request.url.params["url"]
        targets.append(target)
        html = ('<a href="/category.htm">全部商品</a>' if target == SHOP else
                '<a href="https://item.taobao.com/item.htm?id=123">一</a><a class="next" href="/category.htm?page=2">下一页</a>' if "page=2" not in target else
                '<a href="https://item.taobao.com/item.htm?id=123">重复</a><a href="https://item.taobao.com/item.htm?id=456">二</a><div class="recommend"><a href="https://item.taobao.com/item.htm?id=999">广告</a></div>')
        return httpx.Response(200, text=html)
    collector = TaobaoCollector(tmp_path, session=NoBrowserSession(), fetcher=fetcher(handler))
    progress = []
    assert collector.discover_shop("shop", SHOP, on_progress=lambda urls: progress.append(urls)) == [PRODUCT, "https://item.taobao.com/item.htm?id=456"]
    assert targets == [SHOP, SHOP + "category.htm", SHOP + "category.htm?page=2"]
    assert progress[-1] == [PRODUCT, "https://item.taobao.com/item.htm?id=456"]


def test_progress_database_error_does_not_restart_shop_network(tmp_path):
    class DirectSession(NoBrowserSession):
        def iter_shop_pages(self, url):
            yield '<a href="https://item.taobao.com/item.htm?id=123">商品</a>', url, []
    network = fetcher(lambda request: pytest.fail("Database failures must not spend credits"), enabled=False)
    collector = TaobaoCollector(tmp_path, session=DirectSession(), fetcher=network)
    def progress(urls):
        raise OSError("database failed")
    with pytest.raises(OSError, match="database failed"):
        collector.discover_shop("db", SHOP + "category.htm", on_progress=progress)


def test_parser_error_and_evidence_error_never_retry_network(tmp_path, monkeypatch):
    class DirectSession(NoBrowserSession):
        def open(self, url):
            return DETAIL, url, []
        def load_product_details(self):
            return DETAIL, PRODUCT, []
    network = fetcher(lambda request: pytest.fail("Local failures must not fall back"), enabled=False)
    collector = TaobaoCollector(tmp_path, session=DirectSession(), fetcher=network)
    def parse(*args, **kwargs):
        raise ValueError("parser failure")
    monkeypatch.setattr(collector.parser, "parse_product", parse)
    with pytest.raises(ValueError, match="parser failure"):
        collector.collect_product("parse", PRODUCT)
    def save(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(collector, "_save_evidence", save)
    with pytest.raises(OSError, match="disk failure"):
        collector.collect_product("save", PRODUCT)


def test_shop_worker_and_product_children_save_existing_originals_without_login(tmp_path):
    def handler(request):
        target = request.url.params["url"]
        return httpx.Response(200, text='<a href="https://item.taobao.com/item.htm?id=123">商品</a>' if "category.htm" in target else DETAIL)
    application = create_app(tmp_path, start_worker=False)
    with TestClient(application) as client:
        runner = application.state.collector_runner
        runner.collector = TaobaoCollector(tmp_path, session=NoBrowserSession(), fetcher=fetcher(handler))
        image = io.BytesIO()
        Image.new("RGB", (750, 2000), "red").save(image, "PNG")
        real_store = runner.asset_store
        class Store:
            data_dir = real_store.data_dir
            def download_original(self, product_id, url, role, **kwargs):
                assert kwargs["cookies"] == {}
                return real_store.save_original_bytes(product_id, url, role, "image/png", image.getvalue())
        runner.asset_store = Store()
        task_id = client.post("/api/collection/jobs", json={"kind": "shop", "url": SHOP + "category.htm"}).json()["task_id"]
        for _ in range(8):
            if application.state.worker.run_once():
                continue
            with application.state.session_factory.begin() as session:
                for task in session.scalars(select(Task).where(Task.status == TaskStatus.PENDING)):
                    task.available_at = utc_now() - timedelta(seconds=1)
        result = client.get(f"/api/collection/jobs/{task_id}").json()
        assert result["task"]["status"] == TaskStatus.SUCCESS
        assert result["result"]["completed_products"] == 1
        assert result["result"]["detail_count"] == 1
        with application.state.session_factory() as session:
            asset = session.scalar(select(ProductAsset))
            assert asset.role == "detail" and asset.width == 750 and asset.height == 2000
            assert (tmp_path / asset.relative_path).read_bytes() == image.getvalue()
            product = session.scalar(select(Product))
            assert product.asset_status == "READY"


def test_navigation_timeout_after_login_redirect_never_falls_back(tmp_path, monkeypatch):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    class Page:
        url = "https://item.taobao.com/item.htm?id=123"
        def goto(self, *args, **kwargs):
            self.url = "https://login.taobao.com/member/login.jhtml"
            raise PlaywrightTimeoutError("Navigation did not complete")
    session = TaobaoSession(tmp_path)
    session.page = Page()
    monkeypatch.setattr(session, "_start", lambda: None)
    collector = TaobaoCollector(tmp_path, session=session, fetcher=fetcher(
        lambda request: pytest.fail("A login redirect timeout must stay on DIRECT"), enabled=False))
    with pytest.raises(ManualRequired):
        collector.collect_product("redirect", PRODUCT)
