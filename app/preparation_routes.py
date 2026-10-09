"""Preparation UI helpers, kept separate from legacy collection routes."""
import copy
import hashlib
import os
import subprocess
import threading
import uuid
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.preparation_service import validate_directory, setting, get_document, package_history, save_job_package
from app.preparation import FIELD_LABELS, ATTRIBUTE_NAMES, STATUSES
from app.products import Product, CollectionJob, PreparationCapture
from app.product_package import REQUIRED_FILES
from app.taobao.seller_read import read_capabilities, merge_saved_seller_data

PICKER_LOCK = threading.Lock()


def folder_picker_executable():
    root = Path(__file__).resolve().parent.parent
    source = root / 'scripts/ExplorerFolderPicker.cs'
    version = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    executable = root / 'tmp' / ('folder-picker-' + version) / 'FolderPicker.exe'
    if executable.is_file():
        return executable
    compiler = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():
        compiler = compiler.parent.parent.parent / 'Framework/v4.0.30319/csc.exe'
    if not compiler.is_file():
        raise ValueError('Windows 原生文件夹组件无法构建：未找到系统 .NET 编译器')
    executable.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/out:' + str(executable),
                             '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', str(source)],
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or not executable.is_file():
        raise ValueError('无法构建 Windows 文件夹选择组件')
    return executable


def pick_directory(initial):
    if os.name != 'nt':
        raise ValueError('文件夹选择需要在本机 Windows 上运行采集程序')
    if not PICKER_LOCK.acquire(blocking=False):
        raise ValueError('已有文件夹选择窗口，请先完成或取消')
    try:
        # A GUI application hosts the native dialog, independent of PowerShell policy.
        result = subprocess.run([str(folder_picker_executable()), initial or ''], capture_output=True,
                                 encoding='utf-8', errors='replace', timeout=600,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ValueError('无法打开 Windows 文件夹选择窗口，请重试或重启采集程序')
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('文件夹对话框失败或超时，请重新选择') from exc
    finally:
        PICKER_LOCK.release()


class DirectoryRequest(BaseModel):
    path: str = ''


class SavedRecordRequest(BaseModel):
    source: dict


def register_preparation_routes(application, data_dir):
    @application.get('/api/collection/preparation/settings')
    def settings(request: Request):
        with request.app.state.session_factory() as session:
            return {'last_directory': setting(session, 'last_directory').get('path', ''),
                    'field_labels': FIELD_LABELS, 'attribute_names': ATTRIBUTE_NAMES,
                    'statuses': sorted(STATUSES), 'capabilities': read_capabilities()}

    @application.post('/api/collection/preparation/directory/check')
    def directory_check(payload: DirectoryRequest):
        try:
            path = validate_directory(payload.path)
            return {'path': str(path), 'writable': True}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @application.post('/api/collection/preparation/directory/select')
    def directory_select(payload: DirectoryRequest, request: Request):
        if request.client and request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
            raise HTTPException(403, '文件夹对话框只适用于本机访问')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '请从本机采集页面选择目录')
        try:
            selected = pick_directory(payload.path)
            selected = str(validate_directory(selected, check_writable=False)) if selected else None
            writable, error = None, None
            if selected:
                try:
                    validate_directory(selected)
                    writable = True
                except ValueError as exc:
                    writable, error = False, str(exc)
                with request.app.state.session_factory.begin() as session:
                    setting(session, 'last_directory', {'path':selected})
            return {'path': selected, 'canceled': selected is None, 'writable': writable, 'error': error}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @application.post('/api/collection/products/{product_id}/saved-record')
    def saved_record(product_id: str, payload: SavedRecordRequest, request: Request):
        try:
            with request.app.state.session_factory.begin() as session:
                p = session.get(Product, product_id)
                if not p:
                    raise HTTPException(404, '商品不存在')
                d = get_document(session, p)
                # Store a source-only snapshot. Manual overrides remain in their own history.
                d['manual_overrides'] = {}
                d = merge_saved_seller_data(d, payload.source)
                identity = str(uuid.uuid4()); d['capture_id'] = identity
                session.add(PreparationCapture(id=identity, product_id=product_id, kind='saved_record', document=d))
                session.flush()
                return {'document': get_document(session, p), 'note': '本地补充记录已保存；导入值待人工确认。请补存生成新资料包。'}
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from None

    @application.get('/api/collection/jobs/{task_id}/package-file/{filename:path}')
    def package_file(task_id: str, filename: str, request: Request):
        with request.app.state.session_factory() as session:
            job = session.get(CollectionJob, task_id)
            if not job or not job.result_data.get('package_path') or job.result_data.get('package_status') not in {'SAVED', 'INCOMPLETE'}:
                raise HTTPException(404, '尚无资料包')
            root = Path(job.result_data['package_path']).resolve()
            path = (root / filename).resolve()
            is_workbook = path.parent == root and path.suffix.lower() == '.xlsx'
            if (filename not in REQUIRED_FILES | {'package.complete.json', 'save.errors.json', 'provenance.json', '商品资料.xlsx'} and not filename.startswith('assets/') and not is_workbook) or not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in {
                '.html', '.json', '.csv', '.xlsx', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4'}:
                raise HTTPException(404, '资料文件不存在')
        return FileResponse(path)
