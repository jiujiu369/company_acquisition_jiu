"""The shop card exposes the active product instead of a generic stage."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from app.main import create_app


def test_shop_card_shows_names_and_current_download_step(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    stage_at = (datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat()
    shop = {
        "task": {"task_id": "shop", "status": "PENDING"},
        "kind": "shop", "source_url": "https://bolisidg.tmall.com/category.htm",
        "result": {
            "shop_name": "博力斯电工旗舰店", "stage": "正在逐商品采集下载",
            "discovery_complete": False, "product_scraperapi_requests": 1, "scraperapi_requests": 2,
            "total_products": 2, "completed_products": 0, "failed_products": 0, "pending_products": 2,
            "products": [
                {"task_id": "one", "source_url": "https://detail.tmall.com/item.htm?id=123",
                 "status": "RUNNING", "title": "JG电缆压线耳接头铜鼻子",
                 "stage": "正在下载原图 3/8", "stage_at": stage_at, "scraperapi_requests": 1,
                 "scraperapi_render": True, "access_method": "scraperapi"},
                {"task_id": "two", "source_url": "https://detail.tmall.com/item.htm?id=456",
                 "status": "PENDING", "title": None},
            ],
        },
    }
    with TestClient(app) as client, sync_playwright() as playwright:
        html = client.get("/progress").text
        browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        script = Path(__file__).resolve().parents[1] / "static" / "js" / "collection.js"
        css = Path(__file__).resolve().parents[1] / "static" / "css" / "collection.css"

        def serve(route):
            path = urlparse(route.request.url).path
            if path == "/progress":
                route.fulfill(body=html, content_type="text/html")
            elif path == "/static/js/collection.js":
                route.fulfill(body=script.read_text(encoding="utf-8"), content_type="text/javascript")
            elif path == "/static/css/collection.css":
                route.fulfill(body=css.read_text(encoding="utf-8"), content_type="text/css")
            elif path == "/api/collection/jobs":
                route.fulfill(body=json.dumps({"items": [shop], "total": 1}), content_type="application/json")
            elif path == "/api/collection/products":
                route.fulfill(body=json.dumps({"items": [], "total": 0}), content_type="application/json")
            elif path == "/api/collection/network":
                route.fulfill(body=json.dumps({"enabled": True, "configured": True}), content_type="application/json")
            elif path == "/health":
                route.fulfill(body=json.dumps({"worker": "running"}), content_type="application/json")
            else:
                route.fulfill(status=404)

        page.route("**/*", serve)
        page.goto("http://fixture.test/progress")
        card = page.locator(".job").first
        card.wait_for()
        assert card.locator("h3").inner_text() == "博力斯电工旗舰店"
        assert card.locator(".badge").first.inner_text() == "采集中"
        assert "第 1/2 件" in card.locator(".stage").first.inner_text()
        assert "JG电缆压线耳接头铜鼻子" in card.locator(".stage").first.inner_text()
        assert "正在下载原图 3/8" in card.locator(".stage").first.inner_text()
        assert "最近更新" in card.locator(".stage").first.inner_text()
        assert "链接发现尚未结束" in card.inner_text()
        assert "商品请求 1 次" in card.inner_text()
        assert "店铺列表请求 2 次" in card.inner_text()
        card.locator("summary").click()
        assert card.locator(".child strong").first.inner_text() == "JG电缆压线耳接头铜鼻子"
        assert card.locator(".child strong").nth(1).inner_text() == "商品 456（名称待获取）"
        assert "ScraperAPI：1 次" in card.locator('.child').first.inner_text()
        image_path = Path(__file__).resolve().parents[1] / "artifacts" / "collection-progress-ui.png"
        image_path.parent.mkdir(exist_ok=True)
        card.screenshot(path=str(image_path))
        browser.close()
