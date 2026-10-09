from fastapi.testclient import TestClient
from app.main import create_app


def test_server_credentials_are_never_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPERAPI_KEY","test-hidden-credential")
    monkeypatch.setenv("SCRAPERAPI_ENABLED","true")
    with TestClient(create_app(tmp_path,start_worker=False)) as client:
        for url in ("/", "/api/collection/network", "/api/collection/jobs", "/openapi.json"):
            response = client.get(url)
            assert response.status_code == 200
            assert "test-hidden-credential" not in response.text
        assert client.put("/api/collection/network",json={"mode":"direct"}).status_code == 405
        app_scraper = client.app.state.collection_network.scraperapi
        app_scraper.test_connection = lambda: {"success": True, "http_status": 200, "duration_ms": 12,
                                               "exit_ip": "127.0.0.1", "error": None,
                                               "secret": "test-hidden-credential"}
        result = client.post("/api/collection/network/test")
        assert result.status_code == 200
        assert result.json() == {"success": True, "http_status": 200, "duration_ms": 12,
                                 "error": None}
        assert "test-hidden-credential" not in result.text
        app_scraper.test_connection = lambda: {"success": False, "http_status": 401, "duration_ms": 12,
                                               "error": "bad test-hidden-credential"}
        failed = client.post("/api/collection/network/test")
        assert failed.status_code == 200
        assert "test-hidden-credential" not in failed.text
        assert failed.json()["error"] == "bad [已隐藏]"


def test_unconfigured_api_does_not_block_browser_collection_startup(tmp_path,monkeypatch):
    monkeypatch.delenv("SCRAPERAPI_KEY",raising=False)
    monkeypatch.setenv("SCRAPERAPI_ENABLED","true")
    with TestClient(create_app(tmp_path,start_worker=False)) as client:
        assert client.get("/health").status_code == 200
        assert not client.get("/api/collection/network").json()["configured"]
        result = client.post("/api/collection/network/test").json()
        assert result["success"] is False
        assert "未配置" in result["error"]
