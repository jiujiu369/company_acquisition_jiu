"""One visible, persistent buyer-side browser session for manual verification."""

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.taobao import selectors
from app.taobao.actions import BrowserActions
from app.scraperapi_service import DirectAccessError, is_protected_taobao_url, page_access_problem
from app.tasks import TaskNeedsManual


class ManualRequired(TaskNeedsManual):
    def __init__(self, message, *, authentication=True):
        super().__init__(message)
        self.authentication = authentication


class TaobaoSession:
    def __init__(self, data_dir: Path, cdp_url: str | None = None):
        self.data_dir = data_dir
        self.cdp_url = cdp_url or os.getenv("AI_ECOMMERCE_CHROME_CDP_URL")
        self._playwright = None
        self._browser = None
        self.context = None
        self.page = None
        self.manual_pending = False
        self.manual_url = None
        self.network_data: list[dict] = []
        self._pending_requests = set()
        self._shop_source_url = None
        self.actions = BrowserActions()

    def _start(self):
        if self.context:
            try:
                if not self.context.is_closed():
                    if self.page and not self.page.is_closed():
                        return
                    self.page = self.actions.perform("打开浏览器页签", self.context.new_page)
                    self._attach_page_handlers()
                    return
            except PlaywrightError:
                pass
            self.close()
        self._playwright = sync_playwright().start()
        try:
            if self.cdp_url:
                self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
                self.context = self._browser.contexts[0]
            else:
                profile = self.data_dir / "playwright" / "taobao_profile"
                profile.mkdir(parents=True, exist_ok=True)
                self.context = self.actions.perform("打开采集浏览器", self._playwright.chromium.launch_persistent_context,
                    str(profile), headless=False, channel="chrome", accept_downloads=False,
                    viewport={"width": 1360, "height": 900}, locale="zh-CN",
                )
            self.page = self.context.pages[0] if self.context.pages else self.actions.perform("打开浏览器页签", self.context.new_page)
            self._attach_page_handlers()
        except Exception:
            self.close()
            raise

    def _attach_page_handlers(self):
        self.page.on("response", self._capture_response)
        self.page.on("request", self._track_request)
        self.page.on("requestfinished", self._finish_request)
        self.page.on("requestfailed", self._finish_request)

    def _track_request(self, request):
        if request.resource_type in {"xhr", "fetch"}:
            self._pending_requests.add(request)

    def _finish_request(self, request):
        self._pending_requests.discard(request)

    def _capture_response(self, response):
        catalog = any(api in urlparse(response.url).path.lower() for api in selectors.SHOP_CATALOG_APIS)
        if len(self.network_data) >= 30 and not catalog:
            return
        try:
            content_type = response.headers.get("content-type", "").lower()
            if "json" not in content_type and not catalog:
                return
            size = int(response.headers.get("content-length", "0") or "0")
            if size > 2_000_000:
                return
            if "json" in content_type:
                value = response.json()
            else:
                # Mtop also returns JSONP. Decode data without executing JavaScript.
                body = response.text()
                if len(body.encode("utf-8")) > 2_000_000:
                    return
                match = re.fullmatch(r"\s*[\w.$]+\s*\((.*)\)\s*;?\s*", body, re.DOTALL)
                value = json.loads(match.group(1) if match else body)
            if isinstance(value, dict):
                self.network_data.append({"request_url": response.url, "data": value})
        except (ValueError, PlaywrightError):
            pass

    def open(self, url: str, refresh: bool = False) -> tuple[str, str, list[dict]]:
        self._shop_source_url = None
        self._start()
        self.network_data = []
        # A resumed task must revisit its target after the user finishes login.
        # Keeping the challenge page open here makes the next attempt detect the
        # same login redirect again instead of using the updated session.
        response = None
        try:
            response = self.actions.perform("打开页面", self.page.goto, url, wait_until="domcontentloaded", timeout=45000)
        except PlaywrightTimeoutError:
            self._check_manual_navigation()
            raise DirectAccessError("普通访问连接超时") from None
        except PlaywrightError as exc:
            self._check_manual_navigation()
            if any(marker in str(exc) for marker in (
                "net::ERR_CONNECTION_", "net::ERR_TIMED_OUT", "net::ERR_NAME_NOT_RESOLVED",
                "net::ERR_NETWORK_", "net::ERR_INTERNET_DISCONNECTED", "net::ERR_EMPTY_RESPONSE",
                "net::ERR_HTTP_RESPONSE_CODE_FAILURE", "net::ERR_ABORTED",
            )):
                raise DirectAccessError("普通访问网络连接失败") from None
            raise
        self.manual_url = url
        self.page.wait_for_timeout(1200)
        self._check_manual()
        self._check_target(url)
        if response and response.status in {404, 410}:
            self.manual_pending = True
            raise ManualRequired(f"目标页面不存在或已失效（HTTP {response.status}），请检查原地址", authentication=False)
        if response and response.status in {403, 408, 429, 500, 502, 503, 504}:
            raise DirectAccessError(f"普通访问失败（HTTP {response.status}）")
        html = self.page.content()
        problem = page_access_problem(html)
        if problem:
            raise DirectAccessError(problem)
        return html, self.page.url, list(self.network_data)

    def _check_target(self, url):
        """Never attach another product's data to the requested source ID."""
        target, actual = urlparse(url), urlparse(self.page.url)
        if (target.hostname or "") in {"item.taobao.com", "detail.tmall.com"}:
            target_id = parse_qs(target.query).get("id")
            if actual.hostname not in {"item.taobao.com", "detail.tmall.com"} or parse_qs(actual.query).get("id") != target_id:
                self.manual_pending = True
                raise ManualRequired("目标商品未正常打开：页面跳转到其他地址，请检查商品是否已下架或仍需验证", authentication=False)
        elif (target.hostname or "").endswith((".taobao.com", ".tmall.com")) and actual.hostname != target.hostname:
            self.manual_pending = True
            raise ManualRequired("目标店铺未正常打开：页面跳转到其他地址，请检查原店铺地址", authentication=False)

    def _check_manual_navigation(self):
        if is_protected_taobao_url(self.page.url):
            self.manual_pending = True
            raise ManualRequired("淘宝要求登录；请在保留的浏览器窗口手动完成后继续")

    def _check_manual(self):
        self._check_manual_navigation()
        for css in ("#fm-login-id", ".nc-container", "#J_MyVerification", "[class*='captcha']"):
            try:
                if self.page.locator(css).first.is_visible(timeout=300):
                    self.manual_pending = True
                    raise ManualRequired("淘宝要求人工验证；请在浏览器窗口完成后继续")
            except PlaywrightError:
                pass
        body_text = self.page.locator("body").inner_text(timeout=10000)[:1200]
        if any(text in body_text for text in selectors.MANUAL_TEXT):
            self.manual_pending = True
            raise ManualRequired("淘宝页面显示登录或风控验证；请手动处理后继续")
        self.manual_pending = False

    def load_product_details(self) -> tuple[str, str, list[dict]]:
        """Scroll merchant descriptions to load original resources; never capture images."""
        tabs = self.page.get_by_text(re.compile(r"^(商品详情|宝贝详情|商品描述)$"))
        for index in range(min(tabs.count(), 8)):
            node = tabs.nth(index)
            if node.is_visible():
                self.actions.perform_detail("点击详情入口", node.click, timeout=5000)
                self.page.wait_for_timeout(500)
                break
        css = ",".join(selectors.PRODUCT_DETAIL_CONTAINERS)
        for _ in range(12):
            self._check_manual()
            if any(frame.locator(css).count() for frame in self.page.frames):
                break
            self.actions.perform_detail("滚动页面", self.page.mouse.wheel, 0, 800)
            self.page.wait_for_timeout(400)
        fragments = []
        for frame in self.page.frames:
            embedding = frame
            excluded_frame = False
            while embedding.parent_frame is not None:
                element = embedding.frame_element()
                if element.evaluate("(el, css) => Boolean(el.closest(css))", ",".join(selectors.DETAIL_EXCLUDED)):
                    excluded_frame = True
                    break
                embedding = embedding.parent_frame
            if excluded_frame:
                continue
            containers = frame.locator(css)
            frame_fragments = []
            for index in range(containers.count()):
                root = containers.nth(index)
                if root.evaluate("(el, excluded) => Boolean(el.closest(excluded))", ",".join(selectors.DETAIL_EXCLUDED)):
                    continue
                box = root.bounding_box()
                if box and box["width"] > 0 and box["height"] > 0:
                    self.actions.perform_detail("滚动至详情", root.scroll_into_view_if_needed, timeout=5000)
                    self.actions.perform_detail("移动鼠标至详情", root.hover, timeout=5000)
                else:
                    self.actions.perform_detail("滚动至详情", root.evaluate, "el => el.scrollIntoView({block:'start'})")
                stable, previous = 0, None
                for _ in range(80):
                    snapshot = root.evaluate("el => [el.scrollHeight, el.querySelectorAll('img').length, el.innerHTML.length]")
                    at_bottom = root.evaluate("el => el.getBoundingClientRect().bottom <= window.innerHeight")
                    if not at_bottom or previous is None:
                        self.actions.perform_detail("滚动详情", self.page.mouse.wheel, 0, 750)
                    self.page.wait_for_timeout(250)
                    self._check_manual()
                    at_bottom = root.evaluate("el => el.getBoundingClientRect().bottom <= window.innerHeight")
                    stable = stable + 1 if snapshot == previous and at_bottom else 0
                    previous = snapshot
                    if stable >= 9 and not self._pending_requests:
                        break
                else:
                    raise ManualRequired("商品详情加载未结束，请检查页面后继续", authentication=False)
                if frame != self.page.main_frame:
                    frame_fragments.append(root.evaluate("el => el.outerHTML"))
            fragments.extend(frame_fragments)
        self._check_manual()
        return self.page.content() + "".join(fragments), self.page.url, list(self.network_data)

    def iter_shop_pages(self, url: str, *, checkpoint=None, on_checkpoint=None, validate_original=False):
        checkpoint = dict(checkpoint or {})
        on_checkpoint = on_checkpoint or (lambda value: None)
        if checkpoint.get("complete"):
            return
        target = checkpoint.get("next_url") or url
        retained = (not validate_original and self._shop_source_url == url and self.page
                    and not self.page.is_closed() and self.page.url == target)
        # Authentication resumes always validate the original shop using the
        # retained context before returning to an unfinished catalog page.
        if not retained:
            if validate_original and target != url:
                self.open(url)
            self.open(target)
        else:
            self._check_manual()
            self._check_target(url)
        self._shop_source_url = url
        page_index = checkpoint.get("page_index", 0)
        state_only = checkpoint.get("state_only", False)
        # A retained page preserves JS pagination exactly. After a process
        # restart a state-only catalog must rebuild its UI position; durable
        # discovered products and completed downloads remain reusable.
        restore_clicks = page_index if state_only and not retained else 0
        if checkpoint.get("advance_pending") and (retained or state_only):
            restore_clicks += 1
            page_index += 1
        elif checkpoint.get("advance_pending"):
            page_index += 1  # Opening the saved next URL already advanced it.
        for _ in range(restore_clicks):
            node = self._shop_next_page()
            if node is None:
                raise ManualRequired("无法恢复店铺分页位置，请检查全部商品列表后继续", authentication=False)
            self.actions.perform("恢复店铺翻页", node.click, timeout=10000)
            self.page.wait_for_timeout(1000)
            self._check_manual()
        def save_position(*, next_url=None, pending=False, complete=False, state=None):
            on_checkpoint({"mode":"direct", "next_url":None if complete else (next_url or self.page.url),
                           "complete":complete, "page_index":page_index,
                           "state_only":state_only if state is None else state, "advance_pending":pending})
        save_position()
        def unfiltered_catalog():
            parsed = urlparse(self.page.url)
            paths = {"/category.htm", "/search.htm", "/catalog", "/all_item.htm", "/all_items.htm", "/list.htm"}
            filters = {"q", "keyword", "catid", "cid", "categoryid", "seller_cids", "start_price", "end_price"}
            return parsed.path.lower() in paths and not any(
                key.lower() in filters and any(value.strip() for value in values)
                for key, values in parse_qs(parsed.query).items())
        entered_catalog = unfiltered_catalog()
        for css in (() if entered_catalog else selectors.SHOP_ALL_PRODUCTS):
            node = self.page.locator(css).first
            if node.count() and node.is_visible():
                self.actions.perform("点击全部商品", node.click, timeout=10000)
                self.page.wait_for_timeout(1000)
                entered_catalog = unfiltered_catalog()
                break
        if not entered_catalog:
            self.manual_pending = True
            raise ManualRequired("未识别店铺全部商品入口，请打开全部商品列表后继续", authentication=False)
        seen = set()
        for _ in range(500):
            previous, stable, emitted = None, 0, None
            for _ in range(80):
                self._check_manual()
                fingerprint = self.page.locator(",".join(selectors.SHOP_PRODUCT_LINKS)).evaluate_all("nodes => nodes.map(n => n.href)")
                height = self.page.evaluate("document.documentElement.scrollHeight")
                signature = (tuple(fingerprint), height, len(self.network_data))
                resources = (tuple(fingerprint), len(self.network_data))
                if resources != emitted:
                    yield self.page.content(), self.page.url, list(self.network_data)
                    emitted = resources
                at_bottom = self.page.evaluate("window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2")
                if not at_bottom or previous is None:
                    self.actions.perform("滚动商品列表", self.page.mouse.wheel, 0, 1000)
                self.page.wait_for_timeout(300)
                at_bottom = self.page.evaluate("window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2")
                stable = stable + 1 if signature == previous and at_bottom else 0
                previous = signature
                if stable >= 7 and at_bottom and not self._pending_requests:
                    more = next((self.page.locator(css).first for css in selectors.SHOP_LOAD_MORE
                                 if self.page.locator(css).count() and self.page.locator(css).first.is_visible()
                                 and self.page.locator(css).first.is_enabled()), None)
                    if more:
                        self.actions.perform("点击加载更多", more.click, timeout=5000)
                        self.page.wait_for_timeout(800)
                        stable = 0
                    else:
                        break
            else:
                raise ManualRequired("店铺商品加载未结束，请检查页面后继续", authentication=False)
            self._check_manual()
            signature = (self.page.url, tuple(fingerprint))
            if signature in seen:
                raise ManualRequired("店铺翻页未产生新页面，请人工检查分页", authentication=False)
            seen.add(signature)
            yield self.page.content(), self.page.url, list(self.network_data)
            next_page = self._shop_next_page()
            if next_page is None:
                save_position(complete=True)
                return
            href = next_page.get_attribute("href")
            next_url = urljoin(self.page.url, href) if href and not href.startswith(("#", "javascript:")) else self.page.url
            if (urlparse(next_url).hostname != urlparse(url).hostname
                    or is_protected_taobao_url(next_url) or urlparse(next_url).scheme not in {"http", "https"}):
                raise ManualRequired("店铺分页跳转到非目标地址，请人工检查后继续", authentication=False)
            previous_url = self.page.url
            save_position(next_url=next_url, pending=True, state=next_url == previous_url)
            self.network_data = []
            self.actions.perform("翻页", next_page.click, timeout=10000)
            self.page.wait_for_timeout(1000)
            page_index += 1
            state_only = self.page.url == previous_url
            save_position()
        raise ManualRequired("店铺分页超过安全上限，请人工检查后继续", authentication=False)

    def _shop_next_page(self):
        return next((self.page.locator(css).first for css in selectors.SHOP_NEXT_PAGE
                     if self.page.locator(css).count() and self.page.locator(css).first.is_visible()
                     and self.page.locator(css).first.is_enabled()
                     and self.page.locator(css).first.get_attribute("aria-disabled") != "true"
                     and "disabled" not in (self.page.locator(css).first.get_attribute("class") or "")), None)

    def cookies(self, url=None) -> dict[str, str]:
        self._start()
        return {item["name"]: item["value"] for item in (self.context.cookies([url]) if url else self.context.cookies())}

    def close(self):
        try:
            if self.context and not self.cdp_url and not self.context.is_closed():
                self.context.close()
        except PlaywrightError:
            pass
        try:
            if self._browser:
                self._browser.close()
        except PlaywrightError:
            pass
        finally:
            self.context = None
            self.page = None
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
