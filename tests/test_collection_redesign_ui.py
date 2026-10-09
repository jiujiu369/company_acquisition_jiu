"""Browser checks for the separated workflow and shared result window."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from app.main import create_app


def test_three_page_workflow_connection_and_product_window(tmp_path):
    root = Path(__file__).resolve().parents[1]
    product = {
        "product_id": "product-1", "title": "测试商品：双规格", "source_url": "https://detail.tmall.com/item.htm?id=123",
        "source_product_id": "123", "source_platform": "tmall", "image_count": 1,
        "collected_at": "2026-09-29T08:00:00Z",
    }
    detail = {**product, "data": {"price": "29.90", "category": "收纳", "skus": [
        {"name": "蓝色 / 大号", "properties": {"颜色": "蓝色", "尺寸": "大号"}, "price": "29.90", "stock": 8,
         "image_url": "https://img.example.test/blue.jpg"},
        {"name": "红色 / 小号", "properties": {"颜色": "红色", "尺寸": "小号"}, "price": "19.90", "stock": 0,
         "image_url": "https://img.example.test/red.jpg"},
    ]}, "images": [{"id": "asset-1", "role": "sku", "source_url": "https://img.example.test/blue.jpg",
                   "width": 800, "height": 800, "url": "/asset/blue.svg"}]}
    job = {"task": {"task_id": "job-1", "status": "SUCCESS"}, "kind": "product",
           "source_url": product["source_url"], "result": {"title": product["title"], "product_id": product["product_id"],
                                                            "total_asset_count": 1}}
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1365, "height": 850})
        errors = []
        fail_products = {"value": False}
        fail_connection = {"value": False}
        submitted = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            url = urlparse(route.request.url)
            path = url.path
            if path in ("/", "/progress", "/products"):
                route.fulfill(body=client.get(path).text, content_type="text/html")
            elif path.startswith("/static/"):
                file = root / path.lstrip("/")
                route.fulfill(body=file.read_bytes(), content_type="text/css" if path.endswith(".css") else "text/javascript")
            elif path == "/health":
                route.fulfill(json={"worker": "running"})
            elif path == '/api/collection/preparation/settings':
                route.fulfill(json={'last_directory':str(tmp_path)})
            elif path == '/api/collection/preparation/directory/check':
                route.fulfill(json={'path':str(tmp_path),'writable':True})
            elif path == "/api/collection/network":
                route.fulfill(json={"enabled": True, "configured": True})
            elif path == "/api/collection/network/test":
                route.fulfill(json={"success": not fail_connection["value"], "http_status": 503 if fail_connection["value"] else 200,
                                    "duration_ms": 200, "error": "连接暂不可用" if fail_connection["value"] else None})
            elif path == "/api/collection/jobs/batch":
                submitted.append(route.request.post_data_json)
                route.fulfill(status=201, json={"count": 2, "task_ids": ["one", "two"]})
            elif path == "/api/collection/jobs":
                route.fulfill(json={"items": [job], "total": 1})
            elif path == "/api/collection/products":
                if fail_products["value"]:
                    route.fulfill(status=503, json={"detail": "服务暂不可用"})
                    return
                q = parse_qs(url.query).get("q", [""])[0]
                items = [product] if not q or q in product["title"] else []
                route.fulfill(json={"items": items, "total": len(items)})
            elif path == "/api/collection/products/product-1":
                route.fulfill(json=detail)
            elif path == "/asset/blue.svg":
                route.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="#3676ab"/></svg>', content_type="image/svg+xml")
            else:
                route.fulfill(status=404)

        page.route("**/*", serve)
        page.goto("http://fixture.test/")
        assert page.locator('#collection-content').input_value() == 'excel_only'
        assert page.locator('nav a[aria-current="page"]').inner_text() == "商品采集"
        assert page.locator("#recent-collection").count() == 0
        page.locator("#submit-collection").click()
        assert "至少输入一个" in page.locator("#url-error").inner_text()
        page.locator("#test-network").click()
        page.locator("#network-state.success").wait_for()
        assert "连接成功" in page.locator("#network-state").inner_text()
        page.screenshot(path=str(root / "artifacts" / "redesign-collection.png"))
        fail_connection["value"] = True
        page.locator("#test-network").click()
        page.locator("#network-state.failure").wait_for()
        assert "连接暂不可用" in page.locator("#network-status").inner_text()
        page.locator("#source-urls").fill("https://item.taobao.com/item.htm?id=123\nhttps://detail.tmall.com/item.htm?id=456")
        page.locator("#submit-collection").click()
        page.get_by_text("已创建 2 个任务").wait_for()
        assert page.locator("#source-urls").input_value() == ""
        assert submitted[-1]['collect_images'] is False
        page.locator('#collection-content').select_option('with_images')
        page.locator('#source-urls').fill('https://item.taobao.com/item.htm?id=123')
        page.locator('#submit-collection').click()
        page.wait_for_function("document.getElementById('source-urls').value === ''")
        assert submitted[-1]['collect_images'] is True

        for route_path, list_selector in (("/progress", ".job"), ("/products", ".saved-row")):
            page.goto("http://fixture.test" + route_path)
            page.locator(list_selector).first.wait_for()
            assert page.locator("#collection-form").count() == 0
            trigger = page.get_by_role("button", name="查看结果").first
            trigger.click()
            dialog = page.locator("#product-dialog")
            assert dialog.is_visible()
            dialog.locator(".sku-card").first.wait_for()
            assert dialog.locator(".sku-card").count() == 2
            assert "29.90" in dialog.locator(".sku-card").first.inner_text()
            assert "库存" in dialog.locator(".sku-card").first.inner_text()
            assert dialog.locator(".sku-card").first.locator("img").is_visible()
            assert dialog.locator(".sku-card").first.locator(".image-missing").is_hidden()
            assert dialog.locator(".sku-card").nth(1).locator(".image-missing").is_visible()
            assert dialog.locator("#close-product").get_attribute("aria-label") == "关闭商品详情"
            if route_path == "/progress":
                page.screenshot(path=str(root / "artifacts" / "redesign-result-dialog.png"))
            dialog.locator(".image-trigger").first.click()
            assert page.locator("#image-dialog").is_visible()
            page.keyboard.press("Escape")
            assert not page.locator("#image-dialog").is_visible()
            if route_path == "/progress":
                dialog.locator("#close-product").click()
            else:
                page.keyboard.press("Escape")
            assert not dialog.is_visible()
            assert trigger.evaluate("element => document.activeElement === element")
        page.screenshot(path=str(root / "artifacts" / "redesign-products.png"))

        page.locator("#product-search").fill("不存在的商品")
        page.get_by_text("没有符合条件的商品").wait_for()
        page.locator("#product-search").fill("")
        page.locator(".saved-row").first.wait_for()
        fail_products["value"] = True
        page.locator("#refresh-products").click()
        page.get_by_text("读取商品失败：服务暂不可用").wait_for()
        fail_products["value"] = False
        page.locator("#refresh-products").click()
        page.locator(".saved-row").first.wait_for()

        page.set_viewport_size({"width": 375, "height": 812})
        page.goto("http://fixture.test/products")
        page.locator(".saved-row").first.wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_role("button", name="查看结果").first.click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        top_before = page.locator(".dialog-header").bounding_box()["y"]
        page.locator(".dialog-content").evaluate("element => element.scrollTop = element.scrollHeight")
        assert page.locator(".dialog-header").bounding_box()["y"] == top_before
        assert page.locator("#close-product").is_visible()
        page.screenshot(path=str(root / "artifacts" / "redesign-products-mobile.png"))
        assert not errors
        browser.close()
