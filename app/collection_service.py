"""Durable collection, browser pacing, and original-only resumable downloads."""

import hashlib
import html
import json
import re
import uuid
from dataclasses import asdict
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import case, func, or_, select

from app.assets import AssetStore
from app.collection_export import write_product_manifest
from app.product_service import save_collected
from app.preparation_service import record_preparation, save_job_package, get_document
from app.products import CollectionJob, Product, ProductAsset
from app.taobao.collector import TaobaoCollector
from app.taobao.parser import ParsedProduct, TaobaoParser, canonical_product_url, validate_shop_url
from app.tasks import Task, TaskDeferred, TaskNeedsManual, TaskPartial, TaskStatus, utc_now

PRODUCT_TASK = "taobao.product"
SHOP_TASK = "taobao.shop"


def source_kind(url):
    url = _resolve_taobao_short_link(url)
    try:
        return "product", canonical_product_url(url)[0]
    except ValueError:
        return "shop", validate_shop_url(url)


def _resolve_taobao_short_link(url: str) -> str:
    """Expand Taobao share links before applying the normal source URL checks."""
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        return url
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or port not in {None, 80, 443}
            or (parsed.hostname or "").lower() != "e.tb.cn"):
        return url
    try:
        with httpx.Client(follow_redirects=False, timeout=12, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(url)
    except httpx.HTTPError:
        raise ValueError("淘宝短链接暂时无法读取，请稍后重试") from None

    if response.is_redirect:
        target = urljoin(url, response.headers.get("location", ""))
        try:
            return canonical_product_url(target)[0]
        except ValueError:
            try:
                return validate_shop_url(target)
            except ValueError:
                raise ValueError("淘宝短链接没有指向有效的商品或店铺页面") from None
    if response.status_code != 200:
        raise ValueError(f"淘宝短链接读取失败（HTTP {response.status_code}）")

    # Taobao's share page may embed its destination in a link, refresh tag, or script.
    # Only accept URLs that pass the same strict product/shop validators as direct input.
    document = response.text[:2_000_000]
    soup = BeautifulSoup(document, "html.parser")
    candidates = [node.get("href", "") for node in soup.select("a[href], link[rel=canonical][href]")]
    for node in soup.select("meta[http-equiv]"):
        if node.get("http-equiv", "").lower() == "refresh":
            match = re.search(r"url\s*=\s*['\"]?([^'\";]+)", node.get("content", ""), re.I)
            if match:
                candidates.append(match.group(1))
    candidates.extend(re.findall(
        r"https?://(?:item\.taobao\.com|detail\.tmall\.com)/item\.htm\?[^\s\"'<>]+",
        document,
        re.I,
    ))
    for candidate in candidates:
        candidate = urljoin(str(response.url), html.unescape(candidate).replace(r"\u0026", "&"))
        try:
            return canonical_product_url(candidate)[0]
        except ValueError:
            try:
                return validate_shop_url(candidate)
            except ValueError:
                continue
    raise ValueError("无法从淘宝短链接识别商品或店铺，请检查链接是否有效")


def valid_original(data_dir, asset):
    path = (Path(data_dir) / asset.relative_path).resolve()
    if not path.is_relative_to(Path(data_dir).resolve()):
        return False
    if not path.is_file() or path.stat().st_size != asset.byte_size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == asset.sha256


def parent_ids(job):
    return list(dict.fromkeys(job.result_data.get("parent_task_ids", []) +
                             ([job.result_data["parent_task_id"]] if job.result_data.get("parent_task_id") else [])))


def retained_parent_tasks(session, job):
    parents = []
    for pid in parent_ids(job):
        parent_job, parent_task = session.get(CollectionJob, pid), session.get(Task, pid)
        if parent_job and parent_task and not parent_job.result_data.get("removed_at"):
            parents.append(parent_task)
    return parents


def collection_needed(session, job):
    """A visible standalone request or another retained shop still needs it."""
    if not job.result_data.get("removed_at") and not job.result_data.get("child_only", bool(parent_ids(job))):
        return True
    return any(parent and not parent.result_data.get("removed_at")
               and job.task_id in parent.result_data.get("child_task_ids", [])
               for pid in parent_ids(job) if (parent := session.get(CollectionJob, pid)))


def remove_collection_jobs(session, jobs):
    """Remove list entries and stop exclusive unfinished work, retaining all data."""
    removed_at = utc_now().isoformat()+"Z"
    candidates = {}
    for job, task in jobs:
        job.result_data = {**job.result_data, "removed_at":removed_at,
                           "child_only":True if job.kind == "product" else job.result_data.get("child_only", False)}
        candidates[task.task_id] = (job, task)
        for cid in job.result_data.get("child_task_ids", []):
            child_job, child = session.get(CollectionJob, cid), session.get(Task, cid)
            if child_job and child:
                candidates[cid] = (child_job, child)
    for job, task in candidates.values():
        if collection_needed(session, job):
            continue
        job.result_data = {**job.result_data, "removed_at":removed_at, "manual_browser_owner":False}
        if task.status in {TaskStatus.PENDING,TaskStatus.RUNNING,TaskStatus.NEEDS_MANUAL}:
            task.status = TaskStatus.CANCELED
            task.available_at = task.lease_until = None
    return len(jobs)


def enqueue_collection(session, *, url: str, kind: str = "auto", data_dir=None, child=False, batch=None, collect_images=True) -> Task:
    url = _resolve_taobao_short_link(url)
    if kind == "auto":
        kind, url = source_kind(url)
    elif kind == "product":
        url = canonical_product_url(url)[0]
    elif kind == "shop":
        url = validate_shop_url(url)
    else:
        raise ValueError("采集类型必须为 product、shop 或 auto")
    urls = [url]
    if kind == "product":
        number = canonical_product_url(url)[1]
        urls = [f"https://{host}/item.htm?id={number}" for host in ("item.taobao.com", "detail.tmall.com")]
    priority = case((Task.status == TaskStatus.SUCCESS, 0),
                    (Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]), 1), else_=2)
    previous_query = select(CollectionJob, Task).join(Task, CollectionJob.task_id == Task.task_id)
    previous_query = previous_query.where(func.coalesce(CollectionJob.preset['collect_images'].as_boolean(), True) == collect_images)
    if batch:
        previous_query = previous_query.where(CollectionJob.preset['batch_id'].as_string() == batch.id)
    else:
        previous_query = previous_query.where(CollectionJob.preset['batch_id'].as_string().is_(None))
    previous = session.execute(previous_query
        .where(CollectionJob.source_url.in_(urls), CollectionJob.kind == kind)
        .order_by(priority if kind == "product" else Task.created_at.desc(), Task.created_at.desc())).first()
    if previous:
        job, task = previous
        if job.result_data.get("removed_at"):
            job.result_data = {**job.result_data, "removed_at":None}
            if task.status not in {TaskStatus.SUCCESS,TaskStatus.RUNNING,TaskStatus.PENDING}:
                task.status, task.available_at, task.error_message = TaskStatus.PENDING, None, None
                job.manual_reason = None
            for cid in job.result_data.get("child_task_ids", []):
                child_job, child_task = session.get(CollectionJob, cid), session.get(Task, cid)
                if child_job and child_task and (child_job.result_data.get("removed_at") or child_task.status == TaskStatus.CANCELED):
                    child_job.result_data = {**child_job.result_data, "removed_at":None}
                    if child_task.status not in {TaskStatus.SUCCESS,TaskStatus.RUNNING,TaskStatus.PENDING}:
                        child_task.status, child_task.available_at, child_task.error_message = TaskStatus.PENDING, None, None
                        child_job.manual_reason = None
        if task.status == TaskStatus.SUCCESS and kind == "product" and data_dir and collect_images:
            product = session.get(Product, job.result_data.get("product_id"))
            assets = [a for a in product.assets if a.original_asset_id is None and a.kind == "image"] if product else []
            valid_assets = [a for a in assets if valid_original(data_dir, a)]
            valid_urls = {a.source_url for a in valid_assets}
            valid_ids = {a.id for a in valid_assets}
            valid_urls.update(u for u, aid in job.result_data.get("image_aliases", {}).items() if aid in valid_ids)
            expected = {i["url"] for i in job.result_data.get("parsed_product", {}).get("images", [])}
            if not valid_assets or (expected and not expected.issubset(valid_urls)):
                task.status, task.available_at, task.error_message = TaskStatus.PENDING, None, None
        if not child:
            job.result_data = {**job.result_data, "child_only":False}
        return task
    task = Task(task_id=str(uuid.uuid4()), task_type=PRODUCT_TASK if kind == "product" else SHOP_TASK,
                status=TaskStatus.PENDING, created_at=utc_now(), retry_count=0)
    session.add(task)
    session.flush()
    preset = {'mode': 'original', 'collect_images': collect_images}
    result = {'child_only': child, 'collect_images': collect_images}
    if batch:
        preset.update(batch_id=batch.id, save_target=batch.target_path, collection_mode=batch.mode)
        result.update(batch_id=batch.id, save_target=batch.target_path, package_status='PENDING', collection_mode=batch.mode)
        if kind == 'product' and batch.mode == 'reuse':
            cached = session.execute(select(CollectionJob, Task).join(Task).where(
                CollectionJob.source_url.in_(urls), CollectionJob.kind == 'product',
                Task.status == TaskStatus.SUCCESS).order_by(Task.finished_at.desc()).limit(1)).first()
            if cached and cached[0].result_data.get('product_id'):
                result.update({key: cached[0].result_data[key] for key in (
                    'product_id', 'parsed_product', 'preparation_capture_id', 'image_aliases', 'access_method')
                    if key in cached[0].result_data})
                result.update(reused=True, reused_from_task_id=cached[0].task_id)
                if collect_images and not cached[0].preset.get('collect_images', True):
                    # A metadata-only checkpoint never proves images were acquired.
                    result.pop('parsed_product', None)
                    result.pop('preparation_capture_id', None)
    session.add(CollectionJob(task_id=task.task_id, kind=kind, source_url=url,
                              preset=preset, result_data=result))
    session.flush()
    return task


@lru_cache(maxsize=256)
def _shop_name_from_snapshot(path: str, modified_ns: int) -> str | None:
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
            return TaobaoParser().parse_shop_name(stream.read(512_000))
    except OSError:
        return None


def _saved_shop_name(data_dir, task_id):
    pages = Path(data_dir) / "playwright" / "pages"
    for path in list(sorted(pages.glob(f"{task_id}-*.html")))[:3]:
        try:
            name = _shop_name_from_snapshot(str(path), path.stat().st_mtime_ns)
        except OSError:
            continue
        if name:
            return name
    return None


def _related_shop_name(session, data_dir, job):
    host = urlparse(job.source_url).hostname
    if not host:
        return None
    related = session.execute(select(CollectionJob.task_id, CollectionJob.result_data)
        .where(CollectionJob.kind == "shop", CollectionJob.task_id != job.task_id,
               CollectionJob.source_url.like(f"https://{host}/%"))
        .order_by(CollectionJob.created_at.desc()).limit(30)).all()
    for task_id, result in related:
        name = result.get("shop_name") or _saved_shop_name(data_dir, task_id)
        if name:
            return name
    return None


def shop_result(session, job, data_dir=None):
    result = dict(job.result_data)
    if not result.get("shop_name") and data_dir:
        result["shop_name"] = (_saved_shop_name(data_dir, job.task_id)
                               or _related_shop_name(session, data_dir, job))
    ids = result.get("child_task_ids", [])
    if not ids:
        return {**result, "discovery_complete": result.get("discovery_complete", False)}
    rows = session.execute(select(CollectionJob, Task).join(Task, CollectionJob.task_id == Task.task_id)
                           .where(Task.task_id.in_(ids))).all()
    by_id = {t.task_id: (j, t) for j, t in rows}
    missing = [child for child, _ in by_id.values() if not child.result_data.get("title")]
    local_ids = {child.result_data["product_id"] for child in missing if child.result_data.get("product_id")}
    source_ids = set()
    for child in missing:
        try:
            source_ids.add(canonical_product_url(child.source_url)[1])
        except ValueError:
            pass
    saved_by_local, saved_by_source = {}, {}
    if local_ids or source_ids:
        saved = session.execute(select(Product.local_product_id, Product.source_platform,
            Product.source_product_id, Product.title).where(or_(
                Product.local_product_id.in_(local_ids), Product.source_product_id.in_(source_ids)))).all()
        saved_by_local = {local_id: title for local_id, _, _, title in saved}
        saved_by_source = {(platform, source_id): title for _, platform, source_id, title in saved}
    products = []
    for task_id in ids:
        if task_id not in by_id:
            continue
        child, task = by_id[task_id]
        try:
            source_id = canonical_product_url(child.source_url)[1]
            platform = "tmall" if "tmall.com" in child.source_url else "taobao"
            saved_title = saved_by_source.get((platform, source_id))
        except ValueError:
            saved_title = None
        products.append({"task_id": task_id, "source_url": child.source_url, "status": task.status,
                         "error": task.error_message or child.manual_reason,
                         **{key: child.result_data.get(key) for key in ("stage", "stage_at", "title", "product_id", "detail_count",
                            "main_count", "sku_image_count", "total_asset_count", "saved_originals", "local_path",
                            'package_status', 'package_path', 'package_error', 'completeness', 'collected_at', 'reused',
                            'scraperapi_requests', 'scraperapi_render', 'access_method', 'browser_fallback_reason') }})
        products[-1]["title"] = (child.result_data.get("title")
            or saved_by_local.get(child.result_data.get("product_id")) or saved_title)
    completed = sum(p["status"] == TaskStatus.SUCCESS for p in products)
    failed = sum(p["status"] in {TaskStatus.FAILED, TaskStatus.PARTIAL} for p in products)
    discovery_complete = result.get('discovery_complete', bool(ids))
    package_complete = discovery_complete and bool(products) and all(p.get('package_status') == 'SAVED' for p in products)
    required_complete = discovery_complete and bool(products) and all((p.get('completeness') or {}).get('complete') for p in products)
    return {**result, 'package_status': 'SAVED' if package_complete else 'INCOMPLETE',
            'completeness': {'complete': required_complete, 'missing': [], 'pending': [], 'label': '采集范围内必填资料'},
            "products": products, "total_products": len(products), "completed_products": completed,
            "failed_products": failed, "pending_products": len(products)-completed-failed,
            "product_scraperapi_requests": sum(p.get("scraperapi_requests") or 0 for p in products),
            "detail_count": sum(p.get("detail_count") or 0 for p in products),
            "total_asset_count": sum(p.get("total_asset_count") or 0 for p in products)}


class CollectionRunner:
    def __init__(self, session_factory, data_dir: Path, collector=None, asset_store=None):
        self.session_factory = session_factory
        self.collector = collector or TaobaoCollector(data_dir)
        self.asset_store = asset_store or AssetStore(data_dir)
        self.stopping = lambda: False

    def _check_paused(self, task_id):
        if self.stopping():
            raise TaskDeferred(0)
        with self.session_factory() as session:
            task, job = session.get(Task, task_id), session.get(CollectionJob, task_id)
            parents = retained_parent_tasks(session, job)
            if task.status != TaskStatus.RUNNING or (parents and job.result_data.get("child_only", True) and all(
                    p and p.status in {TaskStatus.CANCELED, TaskStatus.NEEDS_MANUAL} for p in parents)):
                raise TaskDeferred(1)

    def _stage(self, task_id, stage):
        with self.session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            now = utc_now()
            job.result_data = {**job.result_data, "stage": stage, "stage_at": now.isoformat()+"Z"}
            task = session.get(Task, task_id)
            task.heartbeat_at = now
            task.lease_until = now + timedelta(minutes=5)

    def _check_browser(self, task_id):
        self._check_paused(task_id)
        with self.session_factory.begin() as session:
            rows = session.execute(select(CollectionJob, Task).join(Task, CollectionJob.task_id == Task.task_id)
                .where(Task.status.in_([TaskStatus.NEEDS_MANUAL, TaskStatus.PENDING, TaskStatus.RUNNING]),
                       Task.task_id != task_id)).all()
            if any(job.result_data.get("manual_browser_owner") for job, _ in rows):
                job = session.get(CollectionJob, task_id)
                job.result_data = {**job.result_data, "browser_required":True, "stage":"等待其他任务完成人工登录或验证"}
                blocked = True
            else:
                blocked = False
        if blocked:
            raise TaskDeferred(1)

    def _release_browser(self, task_id):
        with self.session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            if job.result_data.get("manual_browser_owner"):
                job.result_data = {**job.result_data, "manual_browser_owner":False}

    def _api_request(self, task_id, render):
        self._check_paused(task_id)
        with self.session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            job.result_data = {**job.result_data,
                "scraperapi_requests": job.result_data.get("scraperapi_requests", 0) + 1,
                "scraperapi_render": render, "scraperapi_last_request_at": utc_now().isoformat()+"Z"}

    def _browser_fallback(self, task_id, reason):
        with self.session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            job.result_data = {**job.result_data, "browser_fallback_reason": reason}

    def run(self, task_id):
        self._check_paused(task_id)
        with self.session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            kind, url, result = job.kind, job.source_url, dict(job.result_data)
            collect_images = job.preset.get('collect_images', True)
            if kind == "product" and parent_ids(job) and result.get("browser_required") and not any((
                    result.get("parsed_product"), result.get("product_id"), result.get("scraperapi_requests"),
                    result.get("authentication_required"), result.get("manual_browser_owner"),
                    result.get("browser_fallback_reason"), job.manual_reason,
                    result.get("stage") == "等待人工处理")):
                # Older shop jobs copied their own login flag to never-fetched children.
                # Product-specific challenges and durable downloads retain their access mode.
                result["browser_required"] = False
                job.result_data = result
        if isinstance(self.collector, TaobaoCollector):
            self.collector.bind_task(lambda: self._check_paused(task_id), lambda stage: self._stage(task_id, stage),
                                     prefer_browser=result.get("browser_required", False),
                                     browser_check=lambda: self._check_browser(task_id), access_method=result.get("access_method"))
            fetcher = self.collector.fetcher
            self.collector.collect_images = collect_images
            fetcher.collect_images = collect_images
            fetcher.per_product = kind == "product"
            fetcher.require_api = kind == "product" and fetcher.scraperapi.settings.enabled
            fetcher.scraperapi.on_request = lambda target, render, attempt: self._api_request(task_id, render)
            fetcher.on_fallback = lambda reason: self._browser_fallback(task_id, reason)
        if isinstance(self.asset_store, AssetStore):
            self.asset_store.check = lambda: self._check_paused(task_id)
        try:
            return self._shop(task_id, url) if kind == "shop" else self._product(task_id, url)
        except TaskNeedsManual as exc:
            with self.session_factory.begin() as session:
                job = session.get(CollectionJob, task_id)
                job.manual_reason = str(exc)
                owns_browser = (isinstance(self.collector, TaobaoCollector)
                    and self.collector.fetcher.last_mode == "direct"
                    and not (job.kind == "shop" and job.result_data.get("discovery_complete")))
                job.result_data = {**job.result_data, "browser_required": True,
                                   "authentication_required":getattr(exc, "authentication", False) or job.result_data.get("authentication_required", False),
                                   "manual_browser_owner":owns_browser, "stage": "等待人工处理"}
                for parent in retained_parent_tasks(session, job):
                    if parent and parent.status != TaskStatus.CANCELED:
                        parent.status, parent.error_message = TaskStatus.NEEDS_MANUAL, str(exc)
                        session.get(CollectionJob, parent.task_id).manual_reason = str(exc)
            raise

    def _enqueue_shop_products(self, task_id, urls, *, checkpoint=None):
        """Persist discovered links and children together before yielding discovery."""
        self._check_paused(task_id)
        with self.session_factory.begin() as session:
            from app.products import PreparationBatch
            job = session.get(CollectionJob, task_id)
            unique = list(dict.fromkeys(canonical_product_url(u)[0]
                for u in job.result_data.get("discovered_urls", []) + urls))
            ids = list(job.result_data.get("child_task_ids", []))
            queued_urls = set(session.scalars(select(CollectionJob.source_url)
                .where(CollectionJob.task_id.in_(ids)))) if ids else set()
            batch = session.get(PreparationBatch, job.preset.get('batch_id')) if job.preset.get('batch_id') else None
            for candidate in unique:
                if candidate in queued_urls:
                    continue
                child = enqueue_collection(session, url=candidate, kind="product",
                    data_dir=self.asset_store.data_dir, child=True, batch=batch, collect_images=job.preset.get('collect_images', True))
                child_job = session.get(CollectionJob, child.task_id)
                # Authentication belongs to the specific product, never to every shop child.
                child_job.result_data = {**child_job.result_data,
                    "parent_task_ids": list(dict.fromkeys(parent_ids(child_job)+[task_id]))}
                if child.task_id not in ids:
                    ids.append(child.task_id)
            job.result_data = {**job.result_data, "discovered_urls": unique, "child_task_ids": ids,
                "count": len(unique), "collection_strategy": "per_product",
                "shop_name": getattr(self.collector, "shop_name", None) or job.result_data.get("shop_name")}
            if checkpoint is not None:
                job.result_data = {**job.result_data, "discovery_checkpoint": checkpoint}

    def _wait_shop_products(self, task_id):
        with self.session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            result = shop_result(session, job)
            job.result_data = result
        manual = next((p for p in result.get("products", []) if p["status"] == TaskStatus.NEEDS_MANUAL), None)
        if manual:
            raise TaskNeedsManual(manual["error"] or "商品需要人工登录或验证")
        if result.get("pending_products"):
            self._release_browser(task_id)
            self._stage(task_id, "正在逐件请求商品、下载原图并保存资料包")
            raise TaskDeferred(1)
        return result

    def _shop(self, task_id, url):
        with self.session_factory() as session:
            prior = dict(session.get(CollectionJob, task_id).result_data)
        complete = prior.get("discovery_complete", bool(prior.get("child_task_ids")))
        with self.session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            job.result_data = {**job.result_data, "discovery_complete": complete}
        # This also migrates saved links from a paused legacy discovery without losing them.
        self._enqueue_shop_products(task_id, [])
        self._wait_shop_products(task_id)
        if not complete:
            self._stage(task_id, "正在发现店铺商品")
            public_checkpoint = None
            def progress(urls):
                nonlocal public_checkpoint
                self._enqueue_shop_products(task_id, urls, checkpoint=public_checkpoint)
                public_checkpoint = None
                self._wait_shop_products(task_id)
            def checkpoint(value):
                nonlocal public_checkpoint
                if isinstance(self.collector, TaobaoCollector) and self.collector.fetcher.last_mode == "scraperapi":
                    # Commit public-page advancement in the same transaction as its links.
                    public_checkpoint = value
                    return
                with self.session_factory.begin() as session:
                    job = session.get(CollectionJob, task_id)
                    job.result_data = {**job.result_data, "discovery_checkpoint":value}
            urls = (self.collector.discover_shop(task_id, url, on_progress=progress,
                    checkpoint=prior.get("discovery_checkpoint"), on_checkpoint=checkpoint)
                    if isinstance(self.collector, TaobaoCollector) else self.collector.discover_shop(task_id, url))
            self._enqueue_shop_products(task_id, urls, checkpoint=public_checkpoint)
            with self.session_factory() as session:
                unique = session.get(CollectionJob, task_id).result_data["discovered_urls"]
            if not unique:
                raise TaskNeedsManual("未找到店铺商品，请确认全部商品列表及登录状态")
            self._check_paused(task_id)
            with self.session_factory.begin() as session:
                job = session.get(CollectionJob, task_id)
                job.result_data = {**job.result_data, "discovered_urls": unique,
                                   "count": len(unique), "discovery_complete": True, "stage": "正在逐商品采集下载"}
                job.manual_reason = None
            self._release_browser(task_id)
        result = self._wait_shop_products(task_id)
        self._stage(task_id, "部分完成" if result["failed_products"] else "已完成")
        if result["failed_products"]:
            raise TaskPartial(None, f'{result["failed_products"]} 个商品未完成，可重试失败商品')

    def _product(self, task_id, url):
        with self.session_factory() as session:
            job = session.get(CollectionJob, task_id)
            result = dict(job.result_data)
            collect_images = job.preset.get('collect_images', True)
        if not result.get("parsed_product") and result.get("product_id"):
            # Jobs created before checkpoints were introduced already have immutable captures.
            with self.session_factory() as session:
                product = session.get(Product, result["product_id"])
                capture = next((c for c in reversed(product.captures) if c.task_id == task_id), None) if product else None
                raw = capture.raw_data if capture else {}
                if raw.get("images") or (not collect_images and raw.get('sku')):
                    result["parsed_product"] = asdict(ParsedProduct(source_product_id=product.source_product_id,
                        source_url=url, title=raw.get("title") or product.title, price=raw.get("price"),
                        category=raw.get("category"), attributes=raw.get("attributes") or {},
                        skus=raw.get("sku") or [], images=raw.get("images") or [], raw_data=raw))
        if result.get("parsed_product", {}).get("images") or (not collect_images and result.get('parsed_product', {}).get('title')):
            parsed = ParsedProduct(**result["parsed_product"])
            product_id = result["product_id"]
        else:
            self._stage(task_id, "正在获取商品页面")
            parsed = self.collector.collect_product(task_id, url)
            self._release_browser(task_id)
            parsed.videos = []
            if not collect_images:
                parsed.images = [next((image for image in parsed.images if image.get('role') == 'main'), None)]
                parsed.images = [image for image in parsed.images if image]
                parsed.detail_blocks = []
            parsed.raw_data = {**parsed.raw_data, "images": parsed.images, "videos": []}
            self._check_paused(task_id)
            with self.session_factory.begin() as session:
                product, duplicate = save_collected(session, parsed, task_id)
                product_id = product.local_product_id
                job = session.get(CollectionJob, task_id)
                job.result_data = {**job.result_data, "parsed_product": asdict(parsed), "product_id": product_id,
                                   "title": parsed.title, "duplicate": duplicate, "image_count": len(parsed.images),
                                   "access_method": self.collector.fetcher.last_mode if isinstance(self.collector, TaobaoCollector) else "fixture"}
                job.manual_reason = None
        if not collect_images:
            parsed.images = [next((image for image in parsed.images if image.get('role') == 'main'), None)]
            parsed.images = [image for image in parsed.images if image]
            parsed.detail_blocks = []
        with self.session_factory() as session:
            product = session.get(Product, product_id)
            existing = [a for a in product.assets if a.original_asset_id is None and a.kind in {"image", "video"}]
            # Old manifest aliases are durable too: duplicate image URLs need no second download after a restart.
            manifest = Path(self.asset_store.data_dir)/"products"/product_id/"source"/"product.json"
            mapping = {}
            if manifest.is_file():
                try:
                    mapping = {i["source_url"]: i["asset_id"] for i in json.loads(manifest.read_text(encoding="utf-8")).get("images", []) if i.get("asset_id")}
                except (ValueError, OSError, KeyError):
                    pass
            mapping.update(result.get("image_aliases", {}))
            by_url = {a.source_url: a for a in existing if valid_original(self.asset_store.data_dir, a)}
            by_id = {a.id: a for a in by_url.values()}
            by_url.update({u: by_id[aid] for u, aid in mapping.items() if aid in by_id})
        errors, originals, aliases = [], [], {}
        for index, image in enumerate(parsed.images):
            self._check_paused(task_id)
            source_url, role = image["url"], image.get("role", "detail")
            if source_url in by_url:
                aliases[source_url] = by_url[source_url]
                continue
            self._stage(task_id, f"正在下载原图 {index+1}/{len(parsed.images)}" if collect_images else '正在下载 Excel 商品主图')
            try:
                cookies = self.collector.cookies(source_url) if isinstance(self.collector, TaobaoCollector) else self.collector.session.cookies()
                asset = self.asset_store.download_original(product_id, source_url, role, cookies=cookies, referer=url)
                duplicate = next((old for old in by_id.values() if old.sha256 == asset.sha256), None)
                if duplicate:
                    (Path(self.asset_store.data_dir)/asset.relative_path).unlink(missing_ok=True)
                    asset = duplicate
                else:
                    with self.session_factory.begin() as session:
                        session.add(asset)
                    originals.append(asset)
                    by_id[asset.id] = asset
                by_url[source_url] = asset
                aliases[source_url] = asset
                with self.session_factory.begin() as session:
                    job = session.get(CollectionJob, task_id)
                    job.result_data = {**job.result_data, "image_aliases": {**job.result_data.get("image_aliases", {}), source_url:asset.id}}
            except TaskDeferred:
                raise
            except Exception as exc:
                errors.append({"url": source_url, "error": str(exc)})
        self._check_paused(task_id)
        with self.session_factory.begin() as session:
            product, job = session.get(Product, product_id), session.get(CollectionJob, task_id)
            assets = [a for a in product.assets if a.original_asset_id is None and a.kind in {"image", "video"} and valid_original(self.asset_store.data_dir, a)]
            partial = bool(errors) or (collect_images and not parsed.images)
            if collect_images:
                product.asset_status = "PARTIAL" if partial and assets else "MISSING" if partial else "READY"
            else:
                assets = []  # Retained old originals do not count as this task's output.
            job.manual_reason = None
            job.result_data = {**job.result_data, "saved_originals": len(originals), "existing_originals": len(aliases)-len(originals),
                               "asset_errors": errors, "total_asset_count": len(assets), "video_count": sum(a.kind == 'video' for a in assets),
                               "main_count": sum(a.role == "main" for a in assets), "detail_count": sum(a.role == "detail" for a in assets),
                               "sku_image_count": sum(a.role == "sku" for a in assets), "collected_at": product.collected_at.isoformat()+"Z",
                               "local_path": str(Path(self.asset_store.data_dir)/"products"/product_id),
                               "stage": "部分完成" if partial else "已完成", 'collect_images': collect_images}
            write_product_manifest(self.asset_store.data_dir, product, parsed, errors, aliases)
            old_document = get_document(session, product, job.result_data['preparation_capture_id']) if job.result_data.get('preparation_capture_id') else None
            if (not old_document or any(m['status'] != 'obtained' for m in old_document['media'])
                    or old_document.get('include_images', True) != collect_images
                    or (parsed.images and (old_document.get('excel_main_image') or {}).get('status') != 'obtained')):
                capture = record_preparation(session, product, parsed, task_id, aliases,
                    observed_at=old_document['captured_at'] if old_document else None, include_images=collect_images)
                job.result_data = {**job.result_data, 'preparation_capture_id': capture.id}
            doc = get_document(session, product, job.result_data['preparation_capture_id'])
            job.result_data = {**job.result_data, 'completeness': doc['completeness'],
                'collected_at':doc['captured_at'], 'title':doc['effective']['fields']['title']['value'] or parsed.title}
        # Collection is committed before external copies; a save error cannot roll it back.
        save_job_package(self.session_factory, self.asset_store.data_dir, task_id, check=lambda: self._check_paused(task_id))
        if partial:
            raise TaskPartial(product_id, f"{len(errors)} 张原图下载失败" if errors else "未找到商品图片，请检查目标页面后重试")
        return product_id

    def close(self):
        self.collector.close()
