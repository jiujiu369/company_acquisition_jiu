"""Buyer-side collection workflow. Selectors and parsing stay in the adapter."""

from pathlib import Path

from app.scraperapi_service import CollectionFetchService, DirectAccessError, page_access_problem
from app.taobao.actions import BrowserActions
from app.taobao.parser import TaobaoParser, canonical_product_url, validate_shop_url
from app.taobao.session import TaobaoSession


class TaobaoCollector:
    def __init__(self, data_dir: Path, session: TaobaoSession | None = None,
                 parser: TaobaoParser | None = None, fetcher: CollectionFetchService | None = None):
        self.data_dir = data_dir
        self.session = session or TaobaoSession(data_dir)
        self.parser = parser or TaobaoParser()
        self.fetcher = fetcher or CollectionFetchService()
        self.shop_name = None
        self.stage = lambda message: None

    def bind_task(self, check, stage, prefer_browser=False, *, browser_check=None, access_method=None):
        self.fetcher.check = check
        self.fetcher.scraperapi.check = check
        self.fetcher.prefer_browser = prefer_browser
        self.fetcher.last_mode = access_method or "direct"
        self.fetcher.before_browser = browser_check or check
        self.fetcher.stage = stage
        self.stage = stage
        self.session.actions = BrowserActions(check=self.fetcher.before_browser, stage=stage)

    def _save_evidence(self, task_id: str, html: str):
        folder = self.data_dir / "playwright" / "pages"
        folder.mkdir(parents=True, exist_ok=True)
        index = 1
        while (folder / f"{task_id}-{index}.html").exists():
            index += 1
        with (folder / f"{task_id}-{index}.html").open("x", encoding="utf-8") as stream:
            stream.write(html)

    def collect_product(self, task_id: str, url: str):
        canonical, _ = canonical_product_url(url)
        collect_images = getattr(self, 'collect_images', True)
        self.fetcher.collect_images = collect_images
        def direct():
            opened = self.session.open(canonical)
            result = self.session.load_product_details() if collect_images else opened
            problem = page_access_problem(result[0])
            if problem:
                raise DirectAccessError(problem)
            return result
        html, final_url, network_data = self.fetcher.fetch(canonical, direct)
        self.stage("正在解析商品资料")
        self._save_evidence(task_id, html)
        return self.parser.parse_product(html, canonical, network_data, detail_only=False)

    def discover_shop(self, task_id: str, url: str, on_progress=None, *, checkpoint=None, on_checkpoint=None) -> list[str]:
        shop_url = validate_shop_url(url)
        self.shop_name = None
        urls, seen_ids = [], set()
        cursor = dict(checkpoint or {})
        def save_cursor(value):
            cursor.clear()
            cursor.update(value)
            if on_checkpoint:
                on_checkpoint(value)
        def direct():
            if isinstance(self.session, TaobaoSession):
                return self.session.iter_shop_pages(shop_url, checkpoint=cursor, on_checkpoint=save_cursor,
                                                   validate_original=self.fetcher.prefer_browser)
            return self.session.iter_shop_pages(shop_url)
        pages = self.fetcher.iter_shop_pages(shop_url, direct, self.parser,
                                            checkpoint=cursor, on_checkpoint=save_cursor)
        for html, final_url, network_data in pages:
            self.stage("正在解析店铺商品列表")
            self._save_evidence(task_id, html)
            self.shop_name = self.shop_name or self.parser.parse_shop_name(html)
            for candidate in self.parser.discover_products(html, shop_url, network_data):
                _, product_id = canonical_product_url(candidate)
                if product_id not in seen_ids:
                    seen_ids.add(product_id)
                    urls.append(candidate)
            if on_progress:
                on_progress(list(urls))
        return urls

    def cookies(self, url=None) -> dict[str, str]:
        # Endpoint requests are anonymous and must never start or read a login context.
        if self.fetcher.last_mode == "scraperapi":
            return {}
        self.fetcher.before_browser()
        return self.session.cookies(url) if url else self.session.cookies()

    def close(self):
        self.session.close()
