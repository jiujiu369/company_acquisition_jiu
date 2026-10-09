"""Isolated public-page requests and collection fallback; no browser cookies or proxies."""

import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import ScraperAPISettings, get_scraperapi_settings
from app.taobao import selectors
from app.taobao.parser import TaobaoParser
from app.tasks import TaskNeedsManual


log = logging.getLogger(__name__)
API_ENDPOINT = "https://api.scraperapi.com"
CONNECTION_TEST_URL = "https://httpbin.org/ip"
CATALOG_PATHS = {"/category.htm", "/search.htm", "/catalog", "/all_item.htm", "/all_items.htm", "/list.htm"}
SHOP_PATHS = CATALOG_PATHS | {"/", "/index.htm", "/shop/view_shop.htm"}
PRIVATE_MARKERS = ("login", "passport", "captcha", "verify", "verification", "seller", "qianniu", "account", "member", "security")
PRIVATE_HOSTS = {"qn", "i", "my", "myseller", "sell", "trade", "buyertrade", "h5api", "api", "market", "creator", "oauth", "auth"}
PRIVATE_QUERY = {"token", "access_token", "cookie", "session", "sid", "auth", "password", "ticket"}
NETWORK_ERRORS = (TimeoutError, httpx.TransportError)


class DirectAccessError(RuntimeError):
    """Only identified page acquisition/access failures are eligible for fallback."""


class ScraperAPIConfigError(ValueError):
    pass


class ScraperAPIRequestError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class BrowserRequired(ScraperAPIRequestError):
    """Public HTML needs browser capabilities or an authenticated session."""


class ScraperAPITimeoutError(ScraperAPIRequestError):
    pass


def is_protected_taobao_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host in {"taobao.com", "tmall.com"} or host.endswith((".taobao.com", ".tmall.com"))):
        return False
    label = host.split(".")[0]
    return label in PRIVATE_HOSTS or any(marker in label or marker in parsed.path.lower() for marker in PRIVATE_MARKERS)


def is_public_collection_url(url: str) -> bool:
    """Allow known storefront paths only, never authentication/seller routes."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or port not in {None, 80, 443}:
        return False
    label = host.split(".")[0]
    if label in PRIVATE_HOSTS or any(marker in label or marker in parsed.path.lower() for marker in PRIVATE_MARKERS):
        return False
    if PRIVATE_QUERY.intersection(key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)):
        return False
    if host in {"item.taobao.com", "detail.tmall.com"}:
        return parsed.path.lower() == "/item.htm" and parse_qs(parsed.query).get("id", [""])[0].isdigit()
    shop_host = (re.fullmatch(r"shop\d*\.taobao\.com", host) is not None
                 or re.fullmatch(r"[a-z0-9-]+\.tmall\.com", host) is not None)
    return shop_host and parsed.path.lower() in SHOP_PATHS


def is_shop_catalog_url(url: str) -> bool:
    parsed = urlparse(url)
    filters = {"q", "keyword", "catid", "cid", "categoryid", "seller_cids", "start_price", "end_price"}
    return parsed.path.lower() in CATALOG_PATHS and not any(
        key.lower() in filters and any(value.strip() for value in values)
        for key, values in parse_qs(parsed.query).items())


def page_access_problem(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    if not text and not soup.find(["script", "img"]):
        return "目标公开页面异常空白"
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if re.match(r"^(access denied|forbidden|too many requests|service unavailable|拒绝访问|访问被拒绝|访问受限)(?:\b|\s|$)",
                title or text[:1200], flags=re.I):
        return "目标公开页面访问受限"
    return None


@dataclass(frozen=True)
class PublicPage:
    html: str
    url: str
    status_code: int
    duration_ms: int


class ScraperAPIService:
    def __init__(self, settings: ScraperAPISettings | None = None, *, transport=None, sleep=time.sleep):
        self.settings = settings or get_scraperapi_settings()
        self.transport = transport
        self.sleep = sleep
        self.check = lambda: None
        self.on_request = lambda url, render, attempt: None

    @property
    def available(self):
        return self.settings.enabled and bool(self.settings.api_key)

    def status(self) -> dict:
        return {"enabled": self.settings.enabled, "configured": bool(self.settings.api_key),
                "status": "已配置" if self.settings.api_key else "未配置"}

    def _request(self, url: str, *, render=False, max_attempts=None) -> PublicPage:
        if not self.settings.enabled:
            raise ScraperAPIConfigError("ScraperAPI 未启用")
        if not self.settings.api_key:
            raise ScraperAPIConfigError("ScraperAPI 未配置，请在服务器 .env 设置 Key")
        params = {"url": url}
        if render:
            params["render"] = "true"
        started = time.monotonic()
        attempts = max(1, min(3, self.settings.max_attempts if max_attempts is None else max_attempts))
        with httpx.Client(timeout=self.settings.timeout, transport=self.transport,
                          follow_redirects=False, trust_env=False) as client:
            for attempt in range(attempts):
                self.check()
                self.on_request(url, render, attempt + 1)
                log.info("[ScraperAPI] 开始请求 attempt=%s render=%s", attempt + 1, render)
                try:
                    # Official header authentication keeps the key out of httpx request URL logs.
                    response = client.get(API_ENDPOINT, params=params, headers={"x-sapi-api_key": self.settings.api_key})
                except httpx.TimeoutException:
                    log.warning("[ScraperAPI] 请求超时 attempt=%s", attempt + 1)
                    error = ScraperAPITimeoutError("ScraperAPI 请求超时")
                except httpx.TransportError as exc:
                    log.warning("[ScraperAPI] 网络请求失败 type=%s", type(exc).__name__)
                    error = ScraperAPIRequestError("ScraperAPI 请求失败：网络连接异常")
                else:
                    status = response.status_code
                    if status == 200:
                        duration = round((time.monotonic() - started) * 1000)
                        log.info("[ScraperAPI] 请求成功 status=200 duration=%sms", duration)
                        return PublicPage(response.text, url, status, duration)
                    log.warning("[ScraperAPI] 请求失败 status=%s", status)
                    reason = ("认证失败，请检查服务器 Key" if status == 401 else
                              "额度不足或请求受限" if status == 403 else
                              "请求失败")
                    error = ScraperAPIRequestError(f"ScraperAPI {reason}（HTTP {status}）", status_code=status)
                    if status not in {408, 429, 500, 502, 503, 504}:
                        raise error from None
                if attempt + 1 == attempts:
                    raise error from None
                remaining = 2 ** attempt
                while remaining > 0:
                    self.check()
                    interval = min(0.2, remaining)
                    self.sleep(interval)
                    remaining -= interval

    def fetch(self, url: str, *, render=False, max_attempts=None) -> PublicPage:
        if not is_public_collection_url(url):
            raise ScraperAPIConfigError("此页面不属于允许的公开商品或店铺页面，请使用普通访问")
        page = self._request(url, render=render, max_attempts=max_attempts)
        soup = BeautifulSoup(page.html, "html.parser")
        redirects = [node.get("src", "") for node in soup.find_all("iframe")]
        redirects += [node.get("action", "") for node in soup.find_all("form")]
        for meta in soup.find_all("meta"):
            if meta.get("http-equiv", "").lower() == "refresh":
                match = re.search(r"url\s*=\s*['\"]?([^'\"]+)", meta.get("content", ""), re.I)
                if match:
                    redirects.append(match.group(1).strip())
        redirects += re.findall(r"(?:(?:window|top|self|document)\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)", page.html)
        redirects += re.findall(r"(?:(?:window|top|self|document)\.)?location\.(?:replace|assign)\(\s*['\"]([^'\"]+)", page.html)
        if (soup.select_one("#fm-login-id, .nc-container, #J_MyVerification, [class*='captcha']")
                or any(signal in soup.get_text(" ", strip=True)[:1200] for signal in selectors.MANUAL_TEXT)
                or any(is_protected_taobao_url(urljoin(url, target)) for target in redirects if target)):
            raise BrowserRequired("公开页面要求登录或验证，转入保留的浏览器会话")
        problem = page_access_problem(page.html)
        if problem:
            raise ScraperAPIRequestError(f"ScraperAPI 请求失败：{problem}", status_code=page.status_code)
        return page

    def test_connection(self) -> dict:
        result = {**self.status(), "success": False, "http_status": None,
                  "duration_ms": 0, "exit_ip": None, "error": None}
        started = time.monotonic()
        try:
            # The UI probe can run concurrently with collection. It must not inherit
            # a task's pause checks or contribute to that product's request counter.
            probe = ScraperAPIService(self.settings, transport=self.transport, sleep=self.sleep)
            page = probe._request(CONNECTION_TEST_URL)
            result["http_status"] = page.status_code
            payload = json.loads(page.html)
            origin = payload.get("origin") if isinstance(payload, dict) else None
            if not isinstance(origin, str) or not origin:
                raise ValueError("Missing IP")
            for address in origin.split(","):
                ipaddress.ip_address(address.strip())
            result.update(success=True, exit_ip=origin)
        except (ScraperAPIConfigError, ScraperAPIRequestError) as exc:
            result["error"] = str(exc)
            result["http_status"] = getattr(exc, "status_code", None)
        except (ValueError, TypeError):
            result["error"] = "ScraperAPI 测试响应无有效出口 IP"
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        return result


class CollectionFetchService:
    """Public HTML first; browser rendering/interaction/authentication when needed."""

    def __init__(self, scraperapi: ScraperAPIService | None = None):
        self.scraperapi = scraperapi or ScraperAPIService()
        self.last_mode = "direct"
        self.prefer_browser = False
        self.check = lambda: None
        self.before_browser = lambda: None
        self.stage = lambda message: None
        self.per_product = False
        self.collect_images = True
        self.require_api = False
        self.on_fallback = lambda reason: None

    def status(self) -> dict:
        return {**self.scraperapi.status(), "strategy": "public_then_browser"}

    def _use_public(self, url):
        return not self.prefer_browser and self.scraperapi.available and is_public_collection_url(url)

    def _public_product(self, url):
        self.check()
        self.stage("正在通过 ScraperAPI 请求商品页面")
        page = self.scraperapi.fetch(url, render=self.per_product,
                                     max_attempts=1 if self.per_product else None)
        soup = BeautifulSoup(page.html, "html.parser")
        scripted = bool(soup.find("script"))
        if scripted and not self.collect_images:
            try:
                metadata = TaobaoParser().parse_product(page.html, url)
            except ValueError:
                raise BrowserRequired('商品名称或SKU信息需要浏览器渲染') from None
            if not metadata.skus:
                raise BrowserRequired('SKU信息需要浏览器渲染')
        if scripted and self.collect_images and not TaobaoParser().detail_images(soup, url):
            raise BrowserRequired("商品详情需要浏览器渲染")
        return page.html, page.url, []

    def fetch(self, url: str, direct):
        if self.require_api and not self.prefer_browser and not self.scraperapi.available:
            raise ScraperAPIConfigError("ScraperAPI 未配置，当前商品尚未请求；配置后重试")
        if self._use_public(url):
            self.last_mode = "scraperapi"
            try:
                return self._public_product(url)
            except ScraperAPIRequestError as exc:
                if self.require_api and not isinstance(exc, BrowserRequired):
                    raise
                # Errors are already sanitized by the HTTP adapter. No response body is logged.
                log.info("[采集] 公开页面需要浏览器：%s", exc)
                self.on_fallback(str(exc))
                self.stage("ScraperAPI 页面信息不足或要求验证，正在使用保留的浏览器补充")
        self.check()
        self.before_browser()
        self.last_mode = "direct"
        return direct()  # Browser challenges and local errors propagate; no recursive fallback.

    def _public_shop_pages(self, url, parser, checkpoint, on_checkpoint):
        seen = set(checkpoint.get("completed_urls", []))
        if checkpoint.get("mode") == "scraperapi":
            if checkpoint.get("complete"):
                return
            url = checkpoint.get("next_url") or url
        for _ in range(500):
            self.check()
            self.stage("正在通过 ScraperAPI 请求店铺商品列表")
            if url in seen:
                raise TaskNeedsManual("店铺分页地址重复，已暂停，请检查分页后继续")
            seen.add(url)
            page = self.scraperapi.fetch(url)
            soup = BeautifulSoup(page.html, "html.parser")
            if not is_shop_catalog_url(url):
                entry = next((link for link in soup.find_all("a", href=True)
                              if link.get_text(strip=True) in {"全部商品", "所有商品", "全部宝贝", "所有宝贝"}
                              and self._safe_shop_link(url, link["href"])
                              and is_shop_catalog_url(urljoin(url, link["href"]))), None)
                if entry:
                    seen.add(url)
                    url = urljoin(url, entry["href"])
                    on_checkpoint({"mode":"scraperapi", "next_url":url, "completed_urls":list(seen), "complete":False})
                    continue
                raise BrowserRequired("店铺全部商品入口需要浏览器交互")
            if not parser.discover_products(page.html, url) and soup.find("script"):
                raise BrowserRequired("店铺商品列表需要浏览器渲染")
            browser_reason = ("店铺商品列表仍需滚动加载" if soup.select_one(
                '[data-has-more="true"], [data-has-more="1"], [data-infinite-scroll="true"]') else None)
            links = [node for node in soup.find_all(["a", "button"])
                     if (any(label in node.get_text(strip=True) for label in ("下一页", "加载更多"))
                         or "下一页" in node.get("aria-label", "")
                         or {"next", "J_NextPage"}.intersection(node.get("class", [])))
                     and not node.has_attr("disabled") and node.get("aria-disabled") != "true"
                     and "disabled" not in " ".join(node.get("class", []))]
            next_link = next((node for node in links if node.get("href")
                              and self._safe_shop_link(url, node["href"])
                              and is_shop_catalog_url(urljoin(url, node["href"]))), None)
            if links and not next_link:
                browser_reason = browser_reason or "店铺需要浏览器点击加载或翻页"
            next_url = urljoin(url, next_link["href"]) if next_link else None
            # Progress can yield to product jobs. Save the continuation before yielding
            # so this already-parsed page does not spend credits again on resume.
            if browser_reason:
                on_checkpoint({"mode":"direct", "next_url":url, "complete":False})
            else:
                on_checkpoint({"mode":"scraperapi", "next_url":next_url,
                               "completed_urls":list(seen), "complete":not bool(next_url)})
            yield page.html, page.url, []
            if browser_reason:
                raise BrowserRequired(browser_reason)
            if not next_url:
                return
            url = next_url
        raise TaskNeedsManual("店铺分页超过安全上限，已暂停，请人工检查")

    @staticmethod
    def _safe_shop_link(base, href):
        target = urljoin(base, href)
        return urlparse(base).hostname == urlparse(target).hostname and is_public_collection_url(target)

    def iter_shop_pages(self, url, direct, parser, *, checkpoint=None, on_checkpoint=None):
        checkpoint = checkpoint or {}
        on_checkpoint = on_checkpoint or (lambda value: None)
        if checkpoint.get("complete"):
            self.last_mode = checkpoint.get("mode", "scraperapi")
            return
        if self._use_public(url) and checkpoint.get("mode") != "direct":
            self.last_mode = "scraperapi"
            try:
                yield from self._public_shop_pages(url, parser, checkpoint, on_checkpoint)
                return
            except ScraperAPIRequestError as exc:
                log.info("[采集] 公开店铺需要浏览器：%s", exc)
        self.check()
        self.before_browser()
        self.last_mode = "direct"
        yield from direct()
