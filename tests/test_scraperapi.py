"""Public network policy: regressions must never spend credits or leak credentials."""

import logging
from dataclasses import replace

import httpx
import pytest


PRODUCT = "https://item.taobao.com/item.htm?id=123"
HTML = '<h1>商品</h1><div id="J_DivItemDesc"><img src="//img.alicdn.com/detail.jpg"></div>'
TEST_KEY = "unit-test-credential"


def services(handler, *, enabled=True, key=TEST_KEY, attempts=2):
    from app.config import ScraperAPISettings
    from app.scraperapi_service import CollectionFetchService, ScraperAPIService
    settings = ScraperAPISettings(api_key=key, enabled=enabled, max_attempts=attempts)
    api = ScraperAPIService(settings, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    return api, CollectionFetchService(api)


def unexpected_request(request):
    pytest.fail("DIRECT must never request ScraperAPI")


@pytest.mark.parametrize("enabled,key", [(False, TEST_KEY),(True, "")])
def test_browser_remains_usable_when_disabled_or_unconfigured(enabled,key):
    api, fetcher = services(unexpected_request,enabled=enabled,key=key)
    assert fetcher.fetch(PRODUCT,lambda: (HTML,PRODUCT,[])) == (HTML,PRODUCT,[])
    assert fetcher.last_mode == "direct"
    assert "api_key" not in api.status()


def test_forced_scraperapi_uses_isolated_endpoint_without_render_or_cookies(caplog):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, text=HTML)
    api, fetcher = services(handler)
    with caplog.at_level(logging.INFO):
        result = fetcher.fetch(PRODUCT, lambda: pytest.fail("Browser must not start"))
    assert result == (HTML, PRODUCT, [])
    assert fetcher.last_mode == "scraperapi"
    assert len(requests) == 1
    assert requests[0].url.host == "api.scraperapi.com"
    assert requests[0].url.params["url"] == PRODUCT
    assert "render" not in requests[0].url.params
    assert "api_key" not in requests[0].url.params
    assert requests[0].headers["x-sapi-api_key"] == TEST_KEY
    assert "cookie" not in requests[0].headers
    assert TEST_KEY not in caplog.text
    assert TEST_KEY not in repr(api.settings)


@pytest.mark.parametrize("error", [ValueError("parser bug"), OSError("save failed"), RuntimeError("database failed")])
def test_browser_program_errors_propagate_without_retry(error):
    _, fetcher = services(unexpected_request,enabled=False)
    def direct():
        raise error
    with pytest.raises(type(error)) as raised:
        fetcher.fetch(PRODUCT,direct)
    assert raised.value is error


def test_manual_browser_login_never_requests_public_html_again():
    from app.taobao.session import ManualRequired
    _, fetcher = services(unexpected_request)
    fetcher.prefer_browser = True
    def direct():
        raise ManualRequired("请人工登录")
    with pytest.raises(ManualRequired):
        fetcher.fetch(PRODUCT,direct)


@pytest.mark.parametrize("url", [
    "https://login.taobao.com/member/login.jhtml",
    "https://passport.taobao.com/", "https://myseller.taobao.com/",
    "https://qn.taobao.com/", "https://shop123.taobao.com/login.htm",
    "https://shop123.taobao.com/category.htm?token=private",
    "https://detail.tmall.com.evil.test/item.htm?id=123",
    "https://user:pass@shop123.taobao.com/category.htm",
])
def test_protected_or_untrusted_urls_never_reach_scraperapi(url):
    from app.scraperapi_service import ScraperAPIConfigError
    api, _ = services(unexpected_request)
    with pytest.raises(ScraperAPIConfigError):
        api.fetch(url)


def test_auto_protected_url_stays_direct_even_on_failure():
    _, fetcher = services(unexpected_request)
    with pytest.raises(TimeoutError):
        fetcher.fetch("https://login.taobao.com/", lambda: (_ for _ in ()).throw(TimeoutError("timeout")))


def test_auth_failure_is_friendly_and_not_retried_or_logged(caplog):
    from app.scraperapi_service import ScraperAPIRequestError
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401, text=TEST_KEY)
    api, _ = services(handler)
    with caplog.at_level(logging.INFO), pytest.raises(ScraperAPIRequestError, match="认证失败") as raised:
        api.fetch(PRODUCT)
    assert raised.value.status_code == 401
    assert len(calls) == 1
    assert TEST_KEY not in caplog.text + str(raised.value)


def test_transient_http_failure_is_bounded_and_then_succeeds():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429 if len(calls) == 1 else 200, text=HTML)
    api, _ = services(handler)
    assert api.fetch(PRODUCT).html == HTML
    assert len(calls) == 2


def test_timeout_attempts_are_bounded_and_hide_sensitive_exception(caplog):
    from app.scraperapi_service import ScraperAPITimeoutError
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(TEST_KEY, request=request)
    api, _ = services(handler, attempts=3)
    with caplog.at_level(logging.INFO), pytest.raises(ScraperAPITimeoutError, match="超时") as raised:
        api.fetch(PRODUCT)
    assert len(calls) == 3
    assert TEST_KEY not in caplog.text + str(raised.value)
    assert raised.value.__suppress_context__


def test_connection_test_returns_status_duration_and_exit_ip_only():
    def handler(request):
        assert request.url.params["url"] == "https://httpbin.org/ip"
        return httpx.Response(200, json={"origin": "203.0.113.8"})
    api, _ = services(handler)
    result = api.test_connection()
    assert result["enabled"] and result["configured"] and result["success"]
    assert result["http_status"] == 200
    assert result["exit_ip"] == "203.0.113.8"
    assert result["duration_ms"] >= 0
    assert result["error"] is None
    assert TEST_KEY not in str(result)


def test_connection_test_handles_invalid_json_and_unconfigured_service():
    api, _ = services(lambda request: httpx.Response(200, text=TEST_KEY))
    assert not api.test_connection()["success"]
    api, _ = services(unexpected_request, key="")
    result = api.test_connection()
    assert not result["success"] and not result["configured"]
    assert "未配置" in result["error"]


def test_settings_defaults_and_invalid_values_do_not_break_direct(monkeypatch):
    from app.config import get_scraperapi_settings
    for name in ("SCRAPERAPI_KEY", "SCRAPERAPI_ENABLED", "SCRAPERAPI_MODE", "SCRAPERAPI_TIMEOUT", "SCRAPERAPI_MAX_ATTEMPTS"):
        monkeypatch.delenv(name, raising=False)
    settings = get_scraperapi_settings()
    assert not settings.enabled and not settings.api_key 
    monkeypatch.setenv("SCRAPERAPI_MODE", "invalid")
    monkeypatch.setenv("SCRAPERAPI_TIMEOUT", "nan")
    monkeypatch.setenv("SCRAPERAPI_MAX_ATTEMPTS", "900")
    settings = get_scraperapi_settings()
    assert settings.timeout == 70
    assert settings.max_attempts <= 3


def test_html_login_or_captcha_is_not_accepted_as_product():
    from app.scraperapi_service import ScraperAPIRequestError
    api, _ = services(lambda request: httpx.Response(200, text='<form><input id="fm-login-id"></form>'))
    with pytest.raises(ScraperAPIRequestError, match="登录|验证"):
        api.fetch(PRODUCT)


@pytest.mark.parametrize("html", [
    '<title>登录</title><meta http-equiv="refresh" content="0;url=https://login.taobao.com/">',
    '<h1>登录</h1><iframe src="https://login.taobao.com/member/login.jhtml"></iframe>',
    '<title>Login</title><script>location.href="https://login.taobao.com/member/login.jhtml";</script>',
    '<title>Login</title><script>window.location.replace("https://login.taobao.com/member/login.jhtml");</script>',
    '<title>Login</title><script>location.assign("//passport.taobao.com/");</script>',
])
def test_login_redirect_html_is_rejected_without_following_redirect(html):
    from app.scraperapi_service import ScraperAPIRequestError
    api, _ = services(lambda request: httpx.Response(200, text=html))
    with pytest.raises(ScraperAPIRequestError, match="登录|验证"):
        api.fetch(PRODUCT)


def test_access_failure_uses_browser_once_without_exposing_response(caplog):
    requests,visits = [],[]
    def handler(req):
        requests.append(req)
        return httpx.Response(403,text=TEST_KEY)
    _,fetcher = services(handler)
    with caplog.at_level(logging.INFO):
        result=fetcher.fetch(PRODUCT,lambda: (visits.append(True) or HTML,PRODUCT,[]))
    assert result[0] == HTML
    assert len(requests) == len(visits) == 1
    assert TEST_KEY not in caplog.text
