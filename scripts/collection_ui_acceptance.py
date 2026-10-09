"""Real Chrome UI acceptance with isolated records and deterministic image bytes."""

import io
import json
import socket
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import uvicorn
from PIL import Image
from playwright.sync_api import sync_playwright
from sqlalchemy import func, select

from app.main import create_app
from app.taobao.parser import TaobaoParser
from app.taobao.session import ManualRequired
from app.tasks import Task, TaskStatus


PRODUCT = "https://item.taobao.com/item.htm?id=123"
SHOP = "https://shop777.taobao.com/category.htm"
HTML = '<h1>原图采集样例</h1><meta property="og:image" content="https://img.alicdn.com/example.png"><div id="J_DivItemDesc">样例文字前<img src="https://img.alicdn.com/example.png">文字后<img src="https://img.alicdn.com/example.png"></div><script>window.__INITIAL_STATE__={"skus":[{"skuId":"A","properties":{"颜色":"蓝"},"price":"12.00","quantity":0,"imageUrl":"https://img.alicdn.com/example.png"},{"skuId":"B","properties":{"颜色":"红"},"price":"13.00","quantity":10,"imageUrl":"https://img.alicdn.com/example.png"}]};</script>'


class FixtureCollector:
    ready = False
    class Session:
        def cookies(self):
            return {}
    session = Session()
    def collect_product(self, task_id, url):
        if url.endswith("=456") and not self.ready:
            raise ManualRequired("样例页面需要人工登录，完成后继续")
        return TaobaoParser().parse_product(HTML, url)
    def discover_shop(self, task_id, url):
        return [f"https://item.taobao.com/item.htm?id={n}" for n in (456, 789)]
    def close(self):
        pass


def drain(app):
    for _ in range(40):
        if app.state.worker.run_once():
            continue
        with app.state.session_factory.begin() as session:
            pending = list(session.scalars(select(Task).where(Task.status == TaskStatus.PENDING)))
            for task in pending:
                task.available_at = None
        if not pending:
            break


def main():
    evidence = Path("artifacts").resolve()
    evidence.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="collection-ui-") as directory:
        app = create_app(Path(directory), start_worker=False)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "Isolated service did not start"
        collector = FixtureCollector()
        app.state.collector_runner.collector = collector
        original_store = app.state.collector_runner.asset_store
        stream = io.BytesIO()
        Image.new("RGB", (750, 1600), "#347e8b").save(stream, "PNG")
        content = stream.getvalue()
        class Store:
            data_dir = original_store.data_dir
            fail = True
            def download_original(self, product_id, source_url, role, **kwargs):
                with app.state.session_factory() as session:
                    from app.products import Product
                    number = session.get(Product, product_id).source_product_id
                if number == "789" and self.fail:
                    raise RuntimeError("样例原图临时下载失败")
                return original_store.save_original_bytes(product_id, source_url, role, "image/png", content)
        store = Store()
        app.state.collector_runner.asset_store = store
        errors = []
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, channel="chrome")
                page = browser.new_page(viewport={"width":1280,"height":900}, accept_downloads=True)
                page.on("pageerror", lambda error: errors.append(str(error)))
                def action(control, operation):
                    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/"+operation)) as response:
                        control.click()
                    assert response.value.status == 200
                    # Consume the control reply before proceeding. Response.finished()
                    # in this Playwright release leaves a watcher that errors on close.
                    response.value.body()
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                assert page.get_by_role("heading", name="商品采集", exact=True).is_visible()
                page.locator("#source-urls").fill(PRODUCT+"\n"+SHOP)
                page.locator('#save-directory').fill(str(original_store.data_dir))
                page.get_by_role("button", name="开始采集", exact=True).click()
                page.get_by_text("已创建 2 个任务", exact=False).wait_for()
                drain(app)
                page.goto(f"http://127.0.0.1:{port}/progress", wait_until="networkidle")
                page.locator("#refresh-jobs").click()
                shop_card = page.locator(".job").filter(has_text=SHOP)
                shop_card.get_by_role("button", name="人工完成后继续", exact=True).first.wait_for()
                collector.ready = True
                action(shop_card.get_by_role("button", name="人工完成后继续", exact=True).first, "resume")
                drain(app)
                page.locator("#refresh-jobs").click()
                shop_card.get_by_role("button", name="重试失败商品", exact=True).wait_for()
                store.fail = False
                action(shop_card.get_by_role("button", name="重试失败商品", exact=True), "retry")
                drain(app)
                page.locator("#refresh-jobs").click()
                page.wait_for_function("document.querySelector('.job .metric:nth-child(2) strong')?.textContent === '2'")
                shop_card.locator(".children-details > summary").click()
                time.sleep(3.3)
                assert shop_card.locator(".children-details").evaluate("el => el.open"), "Polling collapsed progress"
                fresh = "https://item.taobao.com/item.htm?id=987"
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                page.locator("#source-urls").fill(fresh)
                page.get_by_role("button", name="开始采集", exact=True).click()
                page.get_by_text("已创建 1 个任务", exact=False).wait_for()
                page.goto(f"http://127.0.0.1:{port}/progress", wait_until="networkidle")
                fresh_card = page.locator(".job").filter(has_text=fresh)
                action(fresh_card.get_by_role("button", name="暂停", exact=True), "pause")
                fresh_card.locator(".badge.CANCELED").wait_for()
                action(fresh_card.get_by_role("button", name="继续", exact=True), "resume")
                drain(app)
                page.locator("#refresh-jobs").click()
                fresh_card.get_by_role("button", name="查看结果", exact=True).click()
                page.locator("#product-dialog").wait_for(state="visible")
                page.wait_for_function("document.querySelector('#product-dialog img')?.naturalWidth === 750")
                assert page.locator('.prepared-sku').count()==2
                assert page.get_by_text('总库存',exact=True).first.is_visible()
                page.locator('.manual-editor > summary').click()
                page.locator('#manual-field').select_option('fields.stock')
                page.locator('#manual-state').select_option('obtained')
                page.locator('#manual-value').fill('0')
                action(page.get_by_role('button',name='保存人工补充并生成新资料版本',exact=True),'manual')
                page.wait_for_function("document.querySelector('.preparation-panel')?.textContent.includes('人工修改')")
                page.screenshot(path=str(evidence/'product-preparation-wide.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                assert page.locator('#product-content').evaluate('e=>e.scrollWidth <= e.clientWidth')
                page.screenshot(path=str(evidence/'product-preparation-mobile.png'),full_page=True)
                page.set_viewport_size({'width':1280,'height':900})
                with page.expect_download() as download:
                    page.get_by_role("link", name="下载原图", exact=True).click()
                original = evidence/"collection-only-ui-original.png"
                download.value.save_as(original)
                assert original.read_bytes() == content
                page.locator("#close-product").click()
                with page.expect_download() as download:
                    fresh_card.get_by_role("link", name="原图 ZIP", exact=True).click()
                archive = evidence/"collection-only-ui-export.zip"
                download.value.save_as(archive)
                with zipfile.ZipFile(archive) as z:
                    assert z.read(next(n for n in z.namelist() if n.startswith("images/"))) == content
                    assert {"product.json","product.csv"}.issubset(z.namelist())
                with page.expect_download() as download:
                    page.locator("#export-page").click()
                batch = evidence/"collection-only-ui-batch.zip"
                download.value.save_as(batch)
                with zipfile.ZipFile(batch) as z:
                    images = [n for n in z.namelist() if "/images/" in n]
                    assert len(images) == 4 and all(z.read(n) == content for n in images)
                page.goto(f"http://127.0.0.1:{port}/products", wait_until="networkidle")
                page.locator("#refresh-products").click()
                page.get_by_text("共 4 个商品", exact=False).wait_for()
                page.screenshot(path=str(evidence/"collection-only-ui.png"), full_page=True)
                page.goto(f"http://127.0.0.1:{port}/progress", wait_until="networkidle")
                page.once("dialog", lambda dialog: dialog.dismiss())
                fresh_card.get_by_role("button", name="删除任务", exact=True).click()
                assert fresh_card.count() == 1, "Canceling deletion removed the task"
                page.once("dialog", lambda dialog: dialog.accept())
                with page.expect_response(lambda r: r.request.method == "DELETE" and "/api/collection/jobs/" in r.url) as response:
                    fresh_card.get_by_role("button", name="删除任务", exact=True).click()
                assert response.value.status == 200
                response.value.body()
                fresh_card.wait_for(state="detached")
                page.once("dialog", lambda dialog: dialog.accept())
                with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/jobs/delete")) as response:
                    page.get_by_role("button", name="删除本页", exact=True).click()
                assert response.value.status == 200
                response.value.body()
                page.get_by_text("暂无采集任务。前往“商品采集”添加链接。", exact=True).wait_for()
                assert page.get_by_role("button", name="删除本页", exact=True).is_disabled()
                products = page.request.get(f"http://127.0.0.1:{port}/api/collection/products").json()
                assert products["total"] == 4
                with app.state.session_factory() as session:
                    assert session.scalar(select(func.count()).select_from(Task)) == 5
                for product in products["items"]:
                    result = page.request.get(f"http://127.0.0.1:{port}/api/collection/products/"+product["product_id"]).json()
                    assert page.request.get(f"http://127.0.0.1:{port}"+result["images"][0]["url"]).body() == content
                page.screenshot(path=str(evidence/"collection-tasks-deleted-ui.png"), full_page=True)
                assert errors == [], errors
                page.evaluate("window.dispatchEvent(new Event('pagehide'))")
                page.wait_for_load_state("networkidle")
                browser.close()
            print(json.dumps({"ui":"passed", "products":4, "original_bytes":"exact", "page_errors":errors,
                              "task_delete":"single_cancel_and_batch_passed", "history_records":5,
                              "screenshot":str(evidence/"collection-only-ui.png")}, ensure_ascii=False))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


if __name__ == "__main__":
    main()
