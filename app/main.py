"""One collection/download workflow; existing storage and task history stay durable."""

import logging
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text

from app.config import get_data_dir, prepare_data_dir
from app.collection_service import CollectionRunner, collection_needed, enqueue_collection, remove_collection_jobs, retained_parent_tasks, shop_result, source_kind, valid_original
from app.collection_export import collection_zip, ensure_manifest
from app.database import Base, make_engine, make_session_factory, migrate_collection_schema
from app.products import CollectionJob, Product, ProductAsset
from app.tasks import Task, TaskStatus, TaskWorker, task_to_dict, utc_now
from app.taobao.parser import canonical_product_url
from app.preparation_service import create_batch as create_preparation_batch, get_document, save_manual, save_job_package, package_history, recover_interrupted_saves, validate_directory
from app.preparation_routes import register_preparation_routes
from app.preparation import FIELD_LABELS, ATTRIBUTE_NAMES
from app.export_data import build_export, product_csv_row
from app.product_package import csv_bytes
from app.excel_delivery import excel_download

ROOT = Path(__file__).resolve().parent.parent
COLLECTION_TYPES = {"taobao.product", "taobao.shop"}
def static_versions(request: Request):
    paths=('css/collection.css', 'js/preparation.js', 'js/collection.js')
    return {'asset_versions':{path:hashlib.sha256((ROOT/'static'/path).read_bytes()).hexdigest()[:16]
                              for path in paths}}


templates = Jinja2Templates(directory=ROOT / "templates", context_processors=[static_versions])


class CollectionRequest(BaseModel):
    url: str
    kind: Literal["product", "shop", "auto"] = "auto"
    save_directory: str | None = None
    collection_mode: Literal['reuse', 'refresh'] = 'reuse'
    collect_images: bool = True


class BatchCollectionRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=50)
    save_directory: str | None = None
    collection_mode: Literal['reuse', 'refresh'] = 'reuse'
    collect_images: bool = True


class ManualRequest(BaseModel):
    overrides: dict


class SavePackageRequest(BaseModel):
    save_directory: str | None = None


class ExportRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=50)


class DeleteJobsRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=50)


def public_result(result):
    return {k:v for k,v in result.items() if k not in {"parsed_product", "image_aliases", "parent_task_ids", "child_only", "discovery_checkpoint", "manual_browser_owner"}}


def job_view(session, job, task, data_dir=None):
    return {"task": task_to_dict(task), "kind": job.kind, "source_url": job.source_url,
            "result": public_result(shop_result(session, job, data_dir) if job.kind == "shop" else job.result_data),
            "manual_reason": job.manual_reason}


def create_app(data_dir: Path | None = None, start_worker: bool = True) -> FastAPI:
    data_dir = Path(data_dir).resolve() if data_dir else get_data_dir()

    @asynccontextmanager
    async def lifespan(application):
        prepare_data_dir(data_dir)
        engine = make_engine(data_dir)
        with engine.connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
        Base.metadata.create_all(engine)
        migrate_collection_schema(engine)
        application.state.engine = engine
        application.state.session_factory = make_session_factory(engine)
        recover_interrupted_saves(application.state.session_factory, data_dir)
        # A single-process local worker owns these tasks. Recovery never claims retired business tasks.
        with application.state.session_factory.begin() as session:
            for task in session.scalars(select(Task).where(Task.task_type.in_(COLLECTION_TYPES), Task.status == TaskStatus.RUNNING)):
                task.status, task.lease_until, task.available_at = TaskStatus.PENDING, None, None
        runner = CollectionRunner(application.state.session_factory, data_dir)
        application.state.collector_runner = runner
        application.state.collection_network = runner.collector.fetcher
        worker = TaskWorker(application.state.session_factory,
            handlers={kind:runner.run for kind in COLLECTION_TYPES}, on_stop=runner.close)
        runner.stopping = lambda: worker.stopping
        application.state.worker = worker
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            worker.stop()
            engine.dispose()

    application = FastAPI(title="商品采集下载", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @application.get("/", response_class=HTMLResponse)
    @application.get("/collection", response_class=HTMLResponse)
    def collection_page(request: Request):
        return templates.TemplateResponse(request, "collection.html")

    @application.get("/progress", response_class=HTMLResponse)
    def progress_page(request: Request):
        return templates.TemplateResponse(request, "progress.html")

    @application.get("/products", response_class=HTMLResponse)
    def products_page(request: Request):
        return templates.TemplateResponse(request, "saved_products.html")

    @application.get("/health")
    def health(request: Request):
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"database":"ok", "worker":"running" if request.app.state.worker.alive else "stopped"}

    @application.get("/api/collection/network")
    def network_status(request: Request):
        return request.app.state.collection_network.status()

    @application.post("/api/collection/network/test")
    def network_test(request: Request):
        # Keep credentials and remote response details on the server.
        scraperapi = request.app.state.collection_network.scraperapi
        result = scraperapi.test_connection()
        response = {key: result.get(key) for key in ("success", "http_status", "duration_ms", "error")}
        secret = scraperapi.settings.api_key
        if secret and isinstance(response["error"], str):
            response["error"] = response["error"].replace(secret, "[已隐藏]")
        return response

    @application.post("/api/collection/jobs", status_code=201)
    def create_job(payload: CollectionRequest, request: Request):
        try:
            with request.app.state.session_factory.begin() as session:
                batch = create_preparation_batch(session, payload.save_directory, payload.collection_mode) if payload.save_directory is not None else None
                task = enqueue_collection(session, url=payload.url.strip(), kind=payload.kind, data_dir=data_dir, batch=batch, collect_images=payload.collect_images)
                return {"task_id":task.task_id, "status":task.status}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @application.post("/api/collection/jobs/batch", status_code=201)
    def create_batch(payload: BatchCollectionRequest, request: Request):
        try:
            unique = {}
            for candidate in payload.urls:
                kind, url = source_kind(candidate.strip())
                identity = canonical_product_url(url)[1] if kind == "product" else url
                unique.setdefault((kind, identity), (kind, url))
            sources = list(unique.values())
            with request.app.state.session_factory.begin() as session:
                batch = create_preparation_batch(session, payload.save_directory, payload.collection_mode) if payload.save_directory is not None else None
                tasks = [enqueue_collection(session, url=url, kind=kind, data_dir=data_dir, batch=batch, collect_images=payload.collect_images) for kind,url in sources]
                return {"task_ids":[t.task_id for t in tasks], "count":len(tasks),
                        "items":[{"task_id":t.task_id, "status":t.status} for t in tasks]}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    def get_job(session, task_id):
        job, task = session.get(CollectionJob, task_id), session.get(Task, task_id)
        if not job or not task:
            raise HTTPException(404, "采集任务不存在")
        return job, task

    @application.get("/api/collection/jobs")
    def list_jobs(request: Request, limit: int = 20, offset: int = 0, q: str = "", status: str = "all"):
        if not 1 <= limit <= 50 or offset < 0 or len(q) > 200 or status not in {"all", "PENDING", "RUNNING", "NEEDS_MANUAL", "CANCELED", "SUCCESS", "PARTIAL", "FAILED"}:
            raise HTTPException(422, "分页参数无效")
        with request.app.state.session_factory() as session:
            rows = session.execute(select(CollectionJob, Task).join(Task, CollectionJob.task_id == Task.task_id)
                .order_by(Task.created_at.desc())).all()
            rows = [(j,t) for j,t in rows if not j.result_data.get("removed_at")
                    and not j.result_data.get("child_only", bool(j.result_data.get("parent_task_id")))]
            if status != "all":
                children = {child_id for j,t in rows if j.kind == "shop" and t.status == TaskStatus.PENDING
                            for child_id in j.result_data.get("child_task_ids", [])}
                running = set(session.scalars(select(Task.task_id).where(
                    Task.task_id.in_(children), Task.status == TaskStatus.RUNNING))) if children else set()
                rows = [(j,t) for j,t in rows if (
                    TaskStatus.RUNNING if j.kind == "shop" and t.status == TaskStatus.PENDING
                    and any(child_id in running for child_id in j.result_data.get("child_task_ids", []))
                    else t.status) == status]
            needle = q.strip().casefold()
            if needle:
                rows = [(j,t) for j,t in rows if needle in " ".join((j.source_url,
                    str(j.result_data.get("shop_name") or ""), str(j.result_data.get("title") or ""))).casefold()]
            return {"items":[job_view(session,j,t,data_dir) for j,t in rows[offset:offset+limit]], "total":len(rows), "offset":offset, "limit":limit}

    @application.post("/api/collection/jobs/delete")
    def delete_jobs(payload: DeleteJobsRequest, request: Request):
        with request.app.state.session_factory.begin() as session:
            rows = [get_job(session, tid) for tid in dict.fromkeys(payload.task_ids)]
            count = remove_collection_jobs(session, rows)
        return {"deleted_count":count}

    @application.delete("/api/collection/jobs/{task_id}")
    def delete_job(task_id: str, request: Request):
        with request.app.state.session_factory.begin() as session:
            remove_collection_jobs(session, [get_job(session, task_id)])
        return {"task_id":task_id, "deleted":True}

    @application.get("/api/collection/jobs/{task_id}")
    def get_collection_job(task_id: str, request: Request):
        with request.app.state.session_factory() as session:
            return job_view(session,*get_job(session,task_id),data_dir=data_dir)

    @application.post("/api/collection/jobs/{task_id}/pause")
    def pause(task_id: str, request: Request):
        with request.app.state.session_factory.begin() as session:
            job,task = get_job(session,task_id)
            if job.result_data.get("removed_at") and not collection_needed(session, job):
                raise HTTPException(409, "该任务已删除，请重新添加链接开始采集")
            if task.status not in {TaskStatus.PENDING,TaskStatus.RUNNING,TaskStatus.NEEDS_MANUAL}:
                raise HTTPException(409, "只有进行中的采集任务可以暂停")
            task.status = TaskStatus.CANCELED
            job.result_data = {**job.result_data, "manual_browser_owner":False}
            for cid in job.result_data.get("child_task_ids", []):
                child_job,child = get_job(session,cid)
                parents = retained_parent_tasks(session, child_job)
                if child.status in {TaskStatus.PENDING,TaskStatus.RUNNING,TaskStatus.NEEDS_MANUAL} and child_job.result_data.get("child_only", True) and all(
                        p.status in {TaskStatus.CANCELED,TaskStatus.NEEDS_MANUAL} for p in parents):
                    child.status = TaskStatus.CANCELED
                    child_job.result_data = {**child_job.result_data, "manual_browser_owner":False}
        return {"task_id":task_id, "status":TaskStatus.CANCELED}

    @application.post("/api/collection/jobs/{task_id}/resume")
    @application.post("/api/collection/jobs/{task_id}/retry")
    def continue_job(task_id: str, request: Request):
        retry = request.url.path.endswith("/retry")
        allowed = {TaskStatus.FAILED,TaskStatus.PARTIAL} if retry else {TaskStatus.CANCELED,TaskStatus.NEEDS_MANUAL}
        with request.app.state.session_factory.begin() as session:
            job,task = get_job(session,task_id)
            if job.result_data.get("removed_at") and not collection_needed(session, job):
                raise HTTPException(409, "该任务已删除，请重新添加链接开始采集")
            if task.status not in allowed:
                raise HTTPException(409, "只有失败或部分完成任务可重试" if retry else "只有暂停或等待人工的任务可继续")
            for cid in job.result_data.get("child_task_ids", []):
                child_job,child = get_job(session,cid)
                if child.status in allowed:
                    child.status,child.available_at,child.error_message = TaskStatus.PENDING,None,None
                    child_job.manual_reason = None
                    if retry:
                        child.retry_count += 1
            task.status,task.available_at,task.error_message = TaskStatus.PENDING,None,None
            job.manual_reason = None
            if retry:
                task.retry_count += 1
            for parent in retained_parent_tasks(session, job):
                if parent and parent.status in {TaskStatus.SUCCESS,TaskStatus.PARTIAL,TaskStatus.FAILED}:
                    parent.status,parent.available_at = TaskStatus.PENDING,None
        return {"task_id":task_id, "status":TaskStatus.PENDING}

    def export_products(session, ids):
        product_ids = []
        documents = {}
        for tid in dict.fromkeys(ids):
            job,task = get_job(session,tid)
            jobs = [get_job(session,cid)[0] for cid in job.result_data.get("child_task_ids", [])] if job.kind == "shop" else [job]
            product_ids.extend(j.result_data["product_id"] for j in jobs if j.result_data.get("product_id"))
            for source_job in jobs:
                product = session.get(Product, source_job.result_data.get('product_id')) if source_job.result_data.get('product_id') else None
                if product:
                    doc = get_document(session, product, source_job.result_data.get('preparation_capture_id'))
                    doc['include_images'] = source_job.preset.get('collect_images', True)
                    documents[product.local_product_id] = doc
        products = [session.get(Product,pid) for pid in dict.fromkeys(product_ids)]
        products = [p for p in products if p]
        if not products:
            raise HTTPException(404,"尚无可导出的商品")
        return products, documents

    def export_tasks(session, ids):
        products, documents = export_products(session, ids)
        return collection_zip(data_dir, products, documents)

    def excel_response(products, documents):
        content, filename, mime = excel_download(data_dir, products, documents)
        fallback = 'product.xlsx' if mime != 'application/zip' else 'products-excel.zip'
        return Response(content, media_type=mime, headers={
            'Content-Disposition': f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename)}'})

    @application.get('/api/collection/jobs/{task_id}/export.xlsx')
    def export_job_excel(task_id: str, request: Request):
        with request.app.state.session_factory() as session:
            products, documents = export_products(session, [task_id])
            return excel_response(products, documents)

    @application.post('/api/collection/export.xlsx')
    def export_batch_excel(payload: ExportRequest, request: Request):
        with request.app.state.session_factory() as session:
            products, documents = export_products(session, payload.task_ids)
            return excel_response(products, documents)

    @application.get('/api/collection/products/{product_id}/export.xlsx')
    def export_saved_excel(product_id: str, request: Request):
        with request.app.state.session_factory() as session:
            product = session.get(Product, product_id)
            if not product:
                raise HTTPException(404, '商品不存在')
            return excel_response([product], {product.local_product_id: get_document(session, product)})

    def zip_response(archive, filename):
        def chunks():
            with archive:
                while piece := archive.read(1024*1024):
                    yield piece
        return StreamingResponse(chunks(), media_type="application/zip", headers={"Content-Disposition":f'attachment; filename="{filename}"'})

    @application.get("/api/collection/jobs/{task_id}/export.zip")
    def export_job(task_id: str, request: Request):
        with request.app.state.session_factory() as session:
            archive = export_tasks(session,[task_id])
        return zip_response(archive,"collection.zip")

    @application.post("/api/collection/export.zip")
    def export_batch(payload: ExportRequest, request: Request):
        with request.app.state.session_factory() as session:
            archive = export_tasks(session,payload.task_ids)
        return zip_response(archive,"collection-batch.zip")

    @application.get("/api/collection/jobs/{task_id}/product.{format}")
    def product_file(task_id: str, format: Literal["json","csv"], request: Request):
        with request.app.state.session_factory() as session:
            job,_ = get_job(session,task_id)
            product = session.get(Product,job.result_data.get("product_id")) if job.result_data.get("product_id") else None
            if not product:
                raise HTTPException(404,"商品资料不存在")
            data = build_export(get_document(session, product))
            if format == 'json':
                return JSONResponse(data, headers={'Content-Disposition': 'attachment; filename="product.json"'})
            row = product_csv_row(data)
            return Response(csv_bytes(list(row), [row]), media_type='text/csv; charset=utf-8',
                            headers={'Content-Disposition': 'attachment; filename="product.csv"'})

    @application.get("/api/collection/products")
    def saved_products(request: Request, limit: int = 20, offset: int = 0, q: str = "", platform: str = "all"):
        if not 1 <= limit <= 50 or offset < 0 or len(q) > 200 or platform not in {"all", "taobao", "tmall"}:
            raise HTTPException(422,"分页参数无效")
        with request.app.state.session_factory() as session:
            query = select(Product)
            needle = q.strip().lower()
            if needle:
                escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                query = query.where(or_(func.lower(Product.title).like(pattern, escape="\\"),
                                        Product.source_product_id.like(pattern, escape="\\")))
            if platform != "all":
                query = query.where(Product.source_platform == platform)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(query.order_by(Product.collected_at.desc()).offset(offset).limit(limit)).all()
            return {"items":[{"product_id":p.local_product_id,"title":p.title,"source_url":p.source_url,
                    "source_product_id":p.source_product_id,"source_platform":p.source_platform,
                    "image_count":sum(a.original_asset_id is None and a.kind == "image" for a in p.assets),
                    "collected_at":p.collected_at.isoformat()+"Z"} for p in rows], "total":total}

    @application.get("/api/collection/products/{product_id}")
    def product_result(product_id: str, request: Request):
        with request.app.state.session_factory() as session:
            product = session.get(Product,product_id)
            if not product:
                raise HTTPException(404,"商品不存在")
            return {"product_id":product_id,"source_url":product.source_url,"title":product.title,"data":product.current_data,
                    'preparation': get_document(session, product), 'package_history': package_history(session, product_id),
                    'field_labels': FIELD_LABELS, 'attribute_names': ATTRIBUTE_NAMES,
                    "images":[{"id":a.id,"role":a.role,"source_url":a.source_url,"width":a.width,"height":a.height,
                               "byte_size":a.byte_size,"url":f"/api/collection/products/{product_id}/images/{a.id}"}
                              for a in product.assets if a.original_asset_id is None and a.kind == "image"]}

    @application.get("/api/collection/products/{product_id}/export.zip")
    def export_saved(product_id: str, request: Request):
        with request.app.state.session_factory() as session:
            product = session.get(Product,product_id)
            if not product:
                raise HTTPException(404,"商品不存在")
            archive = collection_zip(data_dir,[product])
        return zip_response(archive,"product.zip")

    @application.get("/api/collection/products/{product_id}/images/{asset_id}")
    def original_file(product_id: str, asset_id: str, request: Request, download: bool = False):
        with request.app.state.session_factory() as session:
            asset = session.get(ProductAsset,asset_id)
            if not asset or asset.product_id != product_id or asset.original_asset_id is not None or asset.kind != "image":
                raise HTTPException(404,"原图不存在")
            path = (data_dir/asset.relative_path).resolve()
            if not path.is_relative_to(data_dir) or not valid_original(data_dir,asset):
                raise HTTPException(404,"原图缺失或校验失败，可重试采集补齐")
            mime = asset.mime_type
        return FileResponse(path,media_type=mime,filename=path.name if download else None)

    @application.get('/api/collection/products/{product_id}/preparation')
    def preparation_result(product_id: str, request: Request):
        with request.app.state.session_factory() as session:
            product = session.get(Product, product_id)
            if not product:
                raise HTTPException(404, '商品不存在')
            return {'document': get_document(session, product), 'package_history': package_history(session, product_id)}

    @application.post('/api/collection/products/{product_id}/manual')
    def manual_result(product_id: str, payload: ManualRequest, request: Request):
        try:
            with request.app.state.session_factory.begin() as session:
                product = session.get(Product, product_id)
                if not product:
                    raise HTTPException(404, '商品不存在')
                doc = save_manual(session, product, payload.overrides)
                ids = [j.task_id for j in session.scalars(select(CollectionJob)).all()
                       if j.kind == 'product' and j.result_data.get('product_id') == product_id and not j.result_data.get('removed_at')
                       and session.get(Task, j.task_id).status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL}]
            saves = [save_job_package(request.app.state.session_factory, data_dir, tid, use_latest=True) for tid in ids]
            return {'document': doc, 'saves': saves}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @application.post('/api/collection/jobs/{task_id}/save-package')
    def resave_package(task_id: str, payload: SavePackageRequest, request: Request):
        try:
            with request.app.state.session_factory.begin() as session:
                job, task = get_job(session, task_id)
                ids = job.result_data.get('child_task_ids', []) if job.kind == 'shop' else [task_id]
                directory = str(validate_directory(payload.save_directory)) if payload.save_directory else None
                if directory:
                    for tid in [task_id, *ids]:
                        child = session.get(CollectionJob, tid)
                        child.preset = {**child.preset, 'save_target':directory}
                available = [tid for tid in ids if session.get(CollectionJob,tid).result_data.get('product_id')]
            items = [save_job_package(request.app.state.session_factory, data_dir, tid, use_latest=True) for tid in available]
            items.extend({'status':'PENDING','task_id':tid,'errors':['商品尚未取得，完成采集后自动保存到任务目录']} for tid in ids if tid not in available)
            if len(items) == 1:
                return items[0]
            return {'status': 'SAVED' if items and all(x['status'] == 'SAVED' for x in items) else 'INCOMPLETE', 'items': items}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    register_preparation_routes(application, data_dir)
    return application


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
