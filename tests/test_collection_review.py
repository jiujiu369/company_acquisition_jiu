"""Regressions confirmed by the independent collection review."""
import io
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import ScraperAPISettings
from app.main import create_app
from app.products import CollectionJob, Product
from app.scraperapi_service import CollectionFetchService, ScraperAPIService
from app.taobao.collector import TaobaoCollector
from app.taobao.parser import ParsedProduct, TaobaoParser
from app.taobao.session import ManualRequired
from app.tasks import Task, TaskDeferred, TaskStatus, utc_now

URL = "https://item.taobao.com/item.htm?id=123"
SECOND = "https://item.taobao.com/item.htm?id=456"
SHOP = "https://shop123.taobao.com/category.htm"
HTML = '<h1>完整商品</h1><div id="J_DivItemDesc"><img src="https://img.alicdn.com/detail.png"></div>'


def network(handler, enabled=True):
    return CollectionFetchService(ScraperAPIService(
        ScraperAPISettings(api_key="isolated-test-key", enabled=enabled, max_attempts=1),
        transport=httpx.MockTransport(handler), sleep=lambda _: None))


class Browser:
    def __init__(self):
        self.visits, self.cookie_reads = [], []
    def open(self, url):
        self.visits.append(url)
        return HTML, url, []
    def load_product_details(self):
        return HTML, URL, []
    def cookies(self, url=None):
        self.cookie_reads.append(url)
        return {"session": "browser-fixture"}
    def close(self):
        pass


def attach_store(app, *, fail=False, expected_cookies=None):
    real = app.state.collector_runner.asset_store
    output = io.BytesIO()
    Image.new("RGB", (8, 30), "blue").save(output, "PNG")
    class Store:
        data_dir = real.data_dir
        def download_original(self, pid, url, role, **kwargs):
            if expected_cookies is not None:
                assert kwargs["cookies"] == expected_cookies
            if fail:
                raise OSError("temporary image failure")
            return real.save_original_bytes(pid, url, role, "image/png", output.getvalue())
    app.state.collector_runner.asset_store = Store()


def test_empty_dynamic_detail_container_requires_browser_even_with_main_image(tmp_path):
    shell = '<h1>商品</h1><div class="tb-gallery"><img src="https://img.alicdn.com/main.png"></div><div id="J_DivItemDesc"></div><script src="/hydrate.js"></script>'
    browser = Browser()
    collector = TaobaoCollector(tmp_path, session=browser,
        fetcher=network(lambda _: httpx.Response(200, text=shell)))
    parsed = collector.collect_product("empty-container", URL)
    assert browser.visits == [URL]
    assert [(i["url"], i["role"]) for i in parsed.images] == [("https://img.alicdn.com/detail.png", "detail")]


@pytest.mark.parametrize("mode", ["scraperapi", "direct"])
def test_restarted_download_checkpoint_restores_its_own_access_mode(tmp_path, mode):
    requests = []
    def handler(req):
        requests.append(req.url.params["url"])
        return httpx.Response(200, text=HTML)
    first = create_app(tmp_path, start_worker=False)
    with TestClient(first) as client:
        collector = TaobaoCollector(tmp_path, session=Browser(), fetcher=network(handler, enabled=mode == "scraperapi"))
        first.state.collector_runner.collector = collector
        attach_store(first, fail=True)
        tid = client.post("/api/collection/jobs", json={"url":URL}).json()["task_id"]
        first.state.worker.run_once()
        assert client.get(f"/api/collection/jobs/{tid}").json()["task"]["status"] == "PARTIAL"
    requests.clear()
    second = create_app(tmp_path, start_worker=False)
    with TestClient(second) as client:
        browser = Browser()
        collector = TaobaoCollector(tmp_path, session=browser,
            fetcher=network(lambda _: pytest.fail("Checkpoint downloads must not fetch pages")))
        collector.fetcher.last_mode = "direct" if mode == "scraperapi" else "scraperapi"
        second.state.collector_runner.collector = collector
        attach_store(second, expected_cookies={} if mode == "scraperapi" else {"session":"browser-fixture"})
        client.post(f"/api/collection/jobs/{tid}/retry")
        second.state.worker.run_once()
        assert client.get(f"/api/collection/jobs/{tid}").json()["task"]["status"] == "SUCCESS"
        assert browser.visits == []
        assert browser.cookie_reads == ([] if mode == "scraperapi" else ["https://img.alicdn.com/detail.png"])


def test_manual_login_reserves_browser_but_allows_unrelated_public_downloads(tmp_path):
    visits, public_requests = [], []
    class ManualBrowser(Browser):
        ready = False
        def open(self, url):
            visits.append(url)
            if url == URL and not self.ready:
                raise ManualRequired("正在人工登录")
            return HTML, url, []
    def handler(req):
        target = req.url.params["url"]
        public_requests.append(target)
        return httpx.Response(200, text=HTML if target.endswith("=789") else '<input id="fm-login-id">')
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        browser = ManualBrowser()
        app.state.collector_runner.collector = TaobaoCollector(tmp_path, session=browser, fetcher=network(handler))
        attach_store(app)
        ids = client.post("/api/collection/jobs/batch", json={"urls":[URL, SECOND, URL.replace("123", "789")]}).json()["task_ids"]
        app.state.worker.run_once()
        app.state.worker.run_once()
        assert visits == [URL], "A queued task navigated away from the user's login page"
        second = client.get(f"/api/collection/jobs/{ids[1]}").json()
        assert second["task"]["status"] == "PENDING"
        assert "人工" in second["result"]["stage"]
        app.state.worker.run_once()
        assert client.get(f"/api/collection/jobs/{ids[2]}").json()["task"]["status"] == "SUCCESS"
        browser.ready = True
        client.post(f"/api/collection/jobs/{ids[0]}/resume")
        app.state.worker.run_once()
        with app.state.session_factory.begin() as session:
            session.get(Task, ids[1]).available_at = None
        app.state.worker.run_once()
        assert visits == [URL, URL, SECOND]
        assert public_requests.count(SECOND) == 1


def test_reacquisition_updates_collection_metadata_and_preserves_first_capture(tmp_path):
    class Collector:
        class Session:
            def cookies(self):
                return {}
        session = Session()
        ready = False
        def collect_product(self, tid, url):
            if not self.ready:
                return TaobaoParser().parse_product('<h1>正在加载</h1>', url)
            return ParsedProduct(source_product_id="123", source_url=url, title="完整商品", price="99",
                attributes={"材质":"棉"}, skus=[{"source_sku_id":"one", "properties":{"颜色":"蓝"}, "price":"99"}],
                images=[{"url":"https://img.alicdn.com/detail.png", "role":"detail"}], raw_data={"title":"完整商品"})
        def close(self):
            pass
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector = Collector()
        app.state.collector_runner.collector = collector
        attach_store(app)
        tid = client.post("/api/collection/jobs", json={"url":URL}).json()["task_id"]
        app.state.worker.run_once()
        with app.state.session_factory() as session:
            original_raw = session.scalar(select(Product)).raw_data.copy()
        collector.ready = True
        client.post(f"/api/collection/jobs/{tid}/retry")
        app.state.worker.run_once()
        with app.state.session_factory() as session:
            product = session.scalar(select(Product))
            assert product.title == product.current_data["title"] == "完整商品"
            assert product.price == Decimal("99")
            assert product.attributes == {"材质":"棉"}
            assert product.current_data["skus"][0]["source_sku_id"] == "one"
            assert len(product.skus) == 1
            assert product.raw_data == original_raw and len(product.captures) == 2


def test_public_shop_resume_keeps_page_cursor_across_restart(tmp_path):
    requests, interrupted = [], True
    def handler(req):
        nonlocal interrupted
        target = req.url.params["url"]
        requests.append(target)
        if "page=2" in target and interrupted:
            interrupted = False
            raise TaskDeferred(1)
        if 'item.htm' in target:
            return httpx.Response(200, text=HTML)
        return httpx.Response(200, text=(f'<a href="{SECOND}">二</a>' if "page=2" in target else
            f'<a href="{URL}">一</a><a class="next" href="/category.htm?page=2">下一页</a>'))
    first = create_app(tmp_path, start_worker=False)
    with TestClient(first) as client:
        first.state.collector_runner.collector = TaobaoCollector(tmp_path, session=Browser(), fetcher=network(handler))
        attach_store(first, expected_cookies={})
        tid = client.post("/api/collection/jobs", json={"url":SHOP}).json()["task_id"]
        first.state.worker.run_once()
        first.state.worker.run_once()  # First product finishes before page two is discovered.
        with first.state.session_factory.begin() as session:
            session.get(Task, tid).available_at = utc_now()-timedelta(seconds=1)
        first.state.worker.run_once()  # Interrupt page two, preserving its cursor.
        client.post(f"/api/collection/jobs/{tid}/pause")
    second = create_app(tmp_path, start_worker=False)
    with TestClient(second) as client:
        second.state.collector_runner.collector = TaobaoCollector(tmp_path, session=Browser(), fetcher=network(handler))
        client.post(f"/api/collection/jobs/{tid}/resume")
        second.state.worker.run_once()
        assert [url for url in requests if 'category.htm' in url] == [SHOP, SHOP + "?page=2", SHOP + "?page=2"]
        assert requests.count(URL) == 1
        assert client.get(f"/api/collection/jobs/{tid}").json()["result"]["discovered_urls"] == [URL, SECOND]


def test_shop_browser_pagination_does_not_force_public_products_into_browser(tmp_path):
    requests = []
    class CatalogBrowser(Browser):
        def iter_shop_pages(self, url):
            yield f'<a href="{URL}">商品</a>', url, []
        def open(self, url):
            pytest.fail("Catalog rendering must not force child products to use the browser")
    def handler(req):
        target = req.url.params["url"]
        requests.append(target)
        return httpx.Response(200, text=f'<a href="{URL}">商品</a><button>加载更多</button>' if target == SHOP else HTML)
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        app.state.collector_runner.collector = TaobaoCollector(tmp_path, session=CatalogBrowser(), fetcher=network(handler))
        attach_store(app, expected_cookies={})
        tid = client.post("/api/collection/jobs", json={"url":SHOP}).json()["task_id"]
        app.state.worker.run_once()
        app.state.worker.run_once()
        with app.state.session_factory() as session:
            child = session.scalar(select(CollectionJob).where(CollectionJob.kind == "product"))
            assert session.get(Task, child.task_id).status == "SUCCESS"
        assert requests == [SHOP, URL]


def test_partial_shop_with_explicit_dynamic_more_signal_requires_browser(tmp_path):
    visits = []
    class CatalogBrowser(Browser):
        def iter_shop_pages(self, url):
            visits.append(url)
            yield f'<a href="{URL}">一</a><a href="{SECOND}">二</a>', url, []
    shell = f'<div data-has-more="true"><a href="{URL}">首屏</a></div><script src="/infinite.js"></script>'
    collector = TaobaoCollector(tmp_path, session=CatalogBrowser(),
        fetcher=network(lambda _: httpx.Response(200, text=shell)))
    assert collector.discover_shop("partial-shop", SHOP) == [URL, SECOND]
    assert visits == [SHOP]


def test_browser_shop_resume_uses_unfinished_page_cursor(tmp_path):
    from playwright.sync_api import sync_playwright
    from app.taobao.session import TaobaoSession
    target = "http://fixture.test/category.htm"
    requests, checkpoints, interrupted = [], [], False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context()
        def route(request):
            requests.append(request.request.url)
            html = (f'<a href="{SECOND}">二</a>' if "page=2" in request.request.url else
                f'<a href="{URL}">一</a><a class="next" href="/category.htm?page=2">下一页</a>')
            request.fulfill(content_type="text/html; charset=utf-8", body=html)
        context.route("**/*", route)
        session = TaobaoSession(tmp_path)
        session.context, session.page = context, context.new_page()
        def checkpoint(value):
            nonlocal interrupted
            checkpoints.append(value)
            if not interrupted and value.get("next_url") == target + "?page=2":
                interrupted = True
                raise TaskDeferred(1)
        try:
            with pytest.raises(TaskDeferred):
                list(session.iter_shop_pages(target, on_checkpoint=checkpoint))
            snapshots = list(session.iter_shop_pages(target, checkpoint=checkpoints[-1], on_checkpoint=checkpoints.append))
            assert requests == [target, target + "?page=2"]
            assert checkpoints[-1]["complete"]
            assert {url for _, url, _ in snapshots} == {target + "?page=2"}
        finally:
            context.close()
            browser.close()


def test_shop_parent_resume_does_not_hold_its_child_login_reservation(tmp_path):
    class CatalogBrowser(Browser):
        ready = False
        def iter_shop_pages(self, url):
            yield f'<a href="{URL}">商品</a>', url, []
        def open(self, url):
            if not self.ready:
                raise ManualRequired("商品需要人工登录")
            return super().open(url)
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        browser = CatalogBrowser()
        app.state.collector_runner.collector = TaobaoCollector(tmp_path, session=browser,
            fetcher=network(lambda _: pytest.fail("Browser-only fixture"), enabled=False))
        attach_store(app)
        tid = client.post("/api/collection/jobs", json={"url":SHOP}).json()["task_id"]
        app.state.worker.run_once()
        app.state.worker.run_once()
        assert client.get(f"/api/collection/jobs/{tid}").json()["task"]["status"] == "NEEDS_MANUAL"
        browser.ready = True
        client.post(f"/api/collection/jobs/{tid}/resume")
        for _ in range(10):
            if app.state.worker.run_once():
                continue
            with app.state.session_factory.begin() as session:
                for task in session.scalars(select(Task).where(Task.status == TaskStatus.PENDING)):
                    task.available_at = None
        assert client.get(f"/api/collection/jobs/{tid}").json()["task"]["status"] == "SUCCESS"


def test_public_empty_shop_does_not_reserve_an_unused_browser(tmp_path):
    class LoginBrowser(Browser):
        ready = False
        def open(self, url):
            if not self.ready:
                raise ManualRequired("需要登录")
            return super().open(url)
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        browser = LoginBrowser()
        app.state.collector_runner.collector = TaobaoCollector(tmp_path, session=browser,
            fetcher=network(lambda req: httpx.Response(200, text='<h1>空店铺</h1>' if "category.htm" in req.url.params["url"] else '<input id="fm-login-id">')))
        attach_store(app)
        ids = client.post("/api/collection/jobs/batch", json={"urls":[URL, SHOP]}).json()["task_ids"]
        app.state.worker.run_once()
        app.state.worker.run_once()
        browser.ready = True
        client.post(f"/api/collection/jobs/{ids[0]}/resume")
        app.state.worker.run_once()
        assert client.get(f"/api/collection/jobs/{ids[0]}").json()["task"]["status"] == "SUCCESS"


@pytest.mark.parametrize("restart", [False, True])
def test_browser_button_pagination_resume_restores_page_state(tmp_path, restart):
    from playwright.sync_api import sync_playwright
    from app.taobao.session import TaobaoSession
    target, requests, checkpoints = "http://fixture.test/category.htm", [], []
    interrupted = False
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context()
        def route(request):
            requests.append(request.request.url)
            request.fulfill(content_type="text/html; charset=utf-8", body=f'''<a id="item" href="{URL}">一</a>
                <button onclick="document.querySelector('#item').href='{SECOND}';this.remove()">下一页</button>''')
        context.route("**/*", route)
        session = TaobaoSession(tmp_path)
        session.context, session.page = context, context.new_page()
        def checkpoint(value):
            nonlocal interrupted
            checkpoints.append(value)
            if not interrupted and value.get("advance_pending"):
                interrupted = True
                raise TaskDeferred(1)
        try:
            with pytest.raises(TaskDeferred):
                list(session.iter_shop_pages(target, on_checkpoint=checkpoint))
            if restart:
                context.close()
                context = browser.new_context()
                context.route("**/*", route)
                session = TaobaoSession(tmp_path)
                session.context, session.page = context, context.new_page()
            snapshots = list(session.iter_shop_pages(target, checkpoint=checkpoints[-1], on_checkpoint=checkpoints.append))
            assert requests == [target] * (2 if restart else 1)
            products = [u for html, _, data in snapshots for u in TaobaoParser().discover_products(html, SHOP, data)]
            assert set(products) == {SECOND}
        finally:
            context.close()
            browser.close()
