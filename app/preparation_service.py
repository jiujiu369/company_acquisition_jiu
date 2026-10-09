"""Durable preparation snapshots, manual revisions and independent save attempts."""
import copy
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from app.products import (CollectionJob, Product, ProductCapture, PreparationCapture, ManualRevision,
                          PreparationBatch, PackageSave, PreparationSetting)
from app.preparation import build_preparation, apply_overrides, use_package_profile
from app.product_package import write_package, verify_package

SAVE_LOCK = threading.RLock()


def validate_directory(path: str, *, check_writable: bool = True) -> Path:
    if not path or not isinstance(path, str) or '\x00' in path:
        raise ValueError('请选择实际保存文件夹')
    root = Path(path).expanduser()
    if not root.is_absolute():
        raise ValueError('保存到须填写完整绝对路径')
    root = root.resolve()
    if not root.is_dir():
        raise ValueError('保存文件夹不存在或不是目录')
    if len(str(root)) > 175:
        raise ValueError('保存路径过长，请选择较短的目录')
    if not check_writable:
        return root
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix='.采集写入检查_', delete=True) as stream:
            stream.write(b'collection-write-check'); stream.flush(); os.fsync(stream.fileno())
    except OSError as exc:
        raise ValueError('保存目录不可写：' + str(exc)) from None
    return root


def setting(session, name, value=None):
    row = session.get(PreparationSetting, name)
    if value is not None:
        if row is None:
            session.add(PreparationSetting(name=name, value=value))
        else:
            row.value = value
        return value
    return row.value if row else {}


def create_batch(session, directory: str, mode: str) -> PreparationBatch:
    if mode not in {'reuse', 'refresh'}:
        raise ValueError('采集模式无效')
    root = validate_directory(directory)
    identity = str(uuid.uuid4())
    target = root / ('批次_' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S') + '_' + identity[:6])
    batch = PreparationBatch(id=identity, root_path=str(root), target_path=str(target), mode=mode)
    session.add(batch)
    setting(session, 'last_directory', {'path': str(root)})
    session.flush()
    return batch


def record_preparation(session, product, parsed, task_id, aliases=None, observed_at=None, include_images=True):
    identity = str(uuid.uuid4())
    doc = build_preparation(product, parsed, aliases)
    if observed_at:
        doc['captured_at']=observed_at
        doc['sources'][0]['observed_at']=observed_at
    previous = session.scalar(select(PreparationCapture).where(
        PreparationCapture.product_id==product.local_product_id, PreparationCapture.kind!='legacy')
        .order_by(PreparationCapture.created_at.desc()).limit(1))
    if previous:
        old = previous.document
        saved_sources = {source['id']:source for source in old['sources'] if source['method']=='user_supplied_saved_record'}
        doc['sources'].extend(copy.deepcopy(list(saved_sources.values())))
        def carry(target, key, original, label):
            if not any(ref in saved_sources for ref in original.get('source_refs', [])):
                return
            if target[key]['status']=='obtained' and target[key]['value']!=original['value']:
                doc['issues'].append({'field':label,'reason':'新公开资料与已保存后台补充记录不同，保留双方待确认',
                    'incoming':copy.deepcopy(target[key]),'previous':copy.deepcopy(original)})
            target[key] = copy.deepcopy(original)
        for key, original in old['collected']['fields'].items():
            carry(doc['collected']['fields'],key,original,'fields.'+key)
        for original in old['collected']['attributes']:
            if not any(ref in saved_sources for ref in original['field'].get('source_refs', [])):
                continue
            attr=next((a for a in doc['collected']['attributes'] if a['source_name']==original['source_name']),None)
            if attr is None:
                doc['collected']['attributes'].append(copy.deepcopy(original))
            else:
                carry(attr,'field',original['field'],'attributes.'+attr['source_name'])
        saved_evidence = {}
        from app.preparation import SKU_FIELDS
        for original in [*old.get('saved_sku_evidence', []), *old['collected']['skus']]:
            if not original.get('source_sku_id'):
                continue
            evidence=saved_evidence.setdefault(str(original['source_sku_id']), {'source_sku_id':original['source_sku_id']})
            for key in SKU_FIELDS:
                if key in original and any(ref in saved_sources for ref in original[key].get('source_refs', [])):
                    evidence[key]=copy.deepcopy(original[key])
        doc['saved_sku_evidence'] = [value for value in saved_evidence.values() if len(value)>1]
        for original in doc['saved_sku_evidence']:
            found=[sku for sku in doc['collected']['skus'] if sku['source_sku_id'] and sku['source_sku_id']==original['source_sku_id']]
            if len(found)==1:
                for key in SKU_FIELDS:
                    if key in original:
                        carry(found[0],key,original[key],'skus.'+found[0]['local_sku_id']+'.'+key)
        from app.preparation import apply_overrides
        doc = apply_overrides(doc, {})
    doc['capture_id'] = identity
    doc['include_images'] = include_images
    doc['excel_main_image'] = next((copy.deepcopy(m) for m in doc['media'] if m['role'] == 'main'), None)
    if not include_images:
        doc['media'] = []
        doc['images'] = []
        doc['detail_blocks'] = []
    row = PreparationCapture(id=identity, product_id=product.local_product_id, task_id=task_id,
                             kind='source', document=doc)
    session.add(row)
    session.flush()
    return row


def get_document(session, product, capture_id=None):
    if capture_id:
        capture = session.get(PreparationCapture, capture_id)
        if not capture or capture.product_id != product.local_product_id:
            raise ValueError('商品准备快照不存在或不匹配')
    else:
        capture = session.scalar(select(PreparationCapture).where(
            PreparationCapture.product_id == product.local_product_id,
            PreparationCapture.kind != 'legacy').order_by(PreparationCapture.created_at.desc()).limit(1))
    doc = copy.deepcopy(capture.document) if capture else build_preparation(product)
    source_skus = ((product.current_data or {}).get('skus') or []) if not capture else []
    if capture and any(not {'name', 'package_quantity', 'package_unit', 'package_price'} <= sku.keys()
                       for sku in doc['collected']['skus']):
        # SKU IDs can retain their identity while labels/pack sizes change. Bind
        # historical projection to that task's immutable source evidence.
        original = session.scalar(select(ProductCapture).where(
            ProductCapture.product_id == product.local_product_id,
            ProductCapture.task_id == capture.task_id).order_by(ProductCapture.captured_at.desc()).limit(1)) if capture.task_id else None
        source_skus = ((original.raw_data or {}).get('sku') or []) if original else []
    doc = use_package_profile(doc, source_skus)
    revision = session.scalar(select(ManualRevision).where(ManualRevision.product_id == product.local_product_id)
                              .order_by(ManualRevision.created_at.desc()).limit(1))
    doc['manual_overrides'] = copy.deepcopy(revision.overrides) if revision else {}
    doc = apply_overrides(doc, {})
    if revision and not any(source['id']=='manual' for source in doc['sources']):
        doc['sources'].append({'id':'manual', 'method':'manual_revision', 'revision_id':revision.id,
            'source_product_id':product.source_product_id, 'url':None, 'observed_at':revision.created_at.isoformat()+'Z'})
    doc['manual_revision_id'] = revision.id if revision else None
    return doc


def save_manual(session, product, overrides):
    if not isinstance(overrides, dict) or len(overrides) > 500:
        raise ValueError('人工修订格式无效')
    values = copy.deepcopy(overrides)
    for path, value in values.items():
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError('人工修订字段须提供状态和值')
            value['source_refs'] = ['manual']
            value['observed_at'] = datetime.now(timezone.utc).isoformat()
    updated = apply_overrides(get_document(session, product), values)
    row = ManualRevision(product_id=product.local_product_id, overrides=updated['manual_overrides'])
    session.add(row); session.flush()
    updated['manual_revision_id'] = row.id
    return updated


def save_job_package(session_factory, data_dir: Path, task_id: str, directory=None, check=None, use_latest=False):
    with SAVE_LOCK:
        with session_factory.begin() as session:
            job = session.get(CollectionJob, task_id)
            if not job or not job.result_data.get('product_id'):
                raise ValueError('尚无可保存商品资料')
            product = session.get(Product, job.result_data['product_id'])
            doc = get_document(session, product, None if use_latest else job.result_data.get('preparation_capture_id'))
            doc['include_images'] = job.preset.get('collect_images', True)
            if use_latest and doc.get('capture_id'):
                job.result_data = {**job.result_data, 'preparation_capture_id': doc['capture_id']}
            target = directory or job.preset.get('save_target')
            if not target:
                target = str(Path(data_dir) / 'prepared')
            if directory:
                target = str(validate_directory(directory))
                job.preset = {**job.preset, 'save_target': target}
            attempt = PackageSave(task_id=task_id, product_id=product.local_product_id, capture_id=doc.get('capture_id'),
                revision_id=doc.get('manual_revision_id'), target_path=target, status='SAVING')
            session.add(attempt); session.flush()
            identity = attempt.id
            job.result_data = {**job.result_data, 'package_status': 'SAVING', 'save_target': target}
            # Snapshot ORM values before releasing the transaction for filesystem work.
            list(product.assets)
            session.expunge(product)
        deferred = None
        try:
            result = write_package(Path(data_dir), product, doc, Path(target), check=check)
        except Exception as exc:
            from app.tasks import TaskDeferred
            if isinstance(exc, TaskDeferred):
                deferred = exc
                result = {'status': 'PENDING', 'path': target, 'errors': ['保存被暂停或程序停止，可继续补存']}
            else:
                result = {'status': 'FAILED', 'path': target, 'errors': [str(exc)]}
        with session_factory.begin() as session:
            row, job = session.get(PackageSave, identity), session.get(CollectionJob, task_id)
            row.status, row.target_path = result['status'], result['path']
            row.error = '\n'.join(result.get('errors', [])) or None
            job.result_data = {**job.result_data, 'package_status': result['status'], 'package_path': result['path'],
                'package_error': row.error, 'package_saved_at': result.get('verified_at'),
                'completeness': doc['completeness'], 'preparation_revision_id': doc.get('manual_revision_id')}
        if deferred:
            raise deferred
        return result


def package_history(session, product_id):
    return [{'task_id': r.task_id, 'capture_id': r.capture_id, 'revision_id': r.revision_id,
             'status': r.status, 'path': r.target_path, 'error': r.error,
             'created_at': r.created_at.isoformat() + 'Z'} for r in session.scalars(select(PackageSave)
             .where(PackageSave.product_id == product_id).order_by(PackageSave.created_at.desc()))]


def recover_interrupted_saves(session_factory, data_dir):
    """Recover local writes only; never revisit product pages or resume paused tasks."""
    from app.tasks import Task, TaskStatus
    ids = []
    with session_factory.begin() as session:
        for attempt in session.scalars(select(PackageSave).where(PackageSave.status == 'SAVING')):
            attempt.status, attempt.error = 'PENDING', '上次保存被程序退出中断，已保留历史文件并安排补存'
            job, task = session.get(CollectionJob, attempt.task_id), session.get(Task, attempt.task_id)
            if job and task and not job.result_data.get('removed_at'):
                job.result_data = {**job.result_data, 'package_status':'PENDING', 'package_error':attempt.error}
                if task.status in {TaskStatus.SUCCESS, TaskStatus.PARTIAL}:
                    ids.append(attempt.task_id)
    for tid in dict.fromkeys(ids):
        save_job_package(session_factory, data_dir, tid)
