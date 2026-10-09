"""Isolate tests from production API credentials and browser wall-clock pacing."""
import pytest


@pytest.fixture(autouse=True)
def collection_environment(monkeypatch):
    monkeypatch.setenv("SCRAPERAPI_ENABLED","false")
    monkeypatch.setenv("SCRAPERAPI_KEY","")
    monkeypatch.setattr("app.taobao.actions.sleep",lambda seconds: None)
