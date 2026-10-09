import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from test_collection_tool import attach, URL


def test_excel_only_collects_just_first_main_image_and_embeds_it(tmp_path):
    from app.taobao.parser import TaobaoParser
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, _ = attach(app)
        html = '<h1>铜端子</h1><meta property="og:image" content="https://img.alicdn.com/main.png">'
        html += '<div id="J_DivItemDesc"><img src="https://img.alicdn.com/detail.png"></div>'
        html += '<script>window.__INITIAL_STATE__=' + json.dumps({'skus': [
            {'skuId': '543211111', 'name': 'JG16-12（20只）', 'price': '32.5'},
            {'skuId': '543222222', 'name': 'JG25-6（20只）', 'price': '39'},
        ]}) + ';</script>'
        collector.collect_product = lambda task, url: TaobaoParser().parse_product(html, url)
        store = app.state.collector_runner.asset_store
        download = store.download_original
        calls = []
        def tracked(*args, **kwargs):
            calls.append(args)
            return download(*args, **kwargs)
        store.download_original = tracked
        tid = client.post('/api/collection/jobs', json={'url': URL, 'collect_images': False}).json()['task_id']
        app.state.worker.run_once()
        result = client.get(f'/api/collection/jobs/{tid}').json()['result']
        assert len(calls) == 1 and calls[0][1] == 'https://img.alicdn.com/main.png'
        assert result['package_status'] == 'SAVED'
        folder = Path(result['package_path'])
        assert not (folder / 'assets').exists()
        for content in ((folder / '铜端子.xlsx').read_bytes(), client.get(f'/api/collection/jobs/{tid}/export.xlsx').content):
            with zipfile.ZipFile(io.BytesIO(content)) as book:
                assert len([n for n in book.namelist() if n.startswith('xl/media/')]) == 1


def test_batch_excel_download_contains_one_workbook_per_product(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        tids = client.post('/api/collection/jobs/batch', json={'urls': [URL, URL.replace('123', '456')],
            'collect_images': False}).json()['task_ids']
        for _ in tids:
            app.state.worker.run_once()
        response = client.post('/api/collection/export.xlsx', json={'task_ids': tids})
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('application/zip')
        assert '.zip' in response.headers['content-disposition']
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            assert len(bundle.namelist()) == 2
            assert bundle.namelist() == ['测试商品.xlsx', '测试商品_2.xlsx']
            assert all(name.endswith('.xlsx') for name in bundle.namelist())
            for name in bundle.namelist():
                with zipfile.ZipFile(io.BytesIO(bundle.read(name))) as book:
                    sheet = book.read('xl/worksheets/sheet1.xml').decode()
                    assert sheet.count('https://item.taobao.com/item.htm?id=') == 1


def test_local_main_image_uses_snapshot_and_never_an_unrelated_sku_image(tmp_path):
    from app.excel_images import main_image_bytes
    output = io.BytesIO()
    Image.new('RGB', (12, 8), 'blue').save(output, 'PNG')
    content = output.getvalue()
    (tmp_path / 'image.png').write_bytes(content)
    asset = SimpleNamespace(id='main', original_asset_id=None, kind='image', role='main',
                            relative_path='image.png', sha256=hashlib.sha256(content).hexdigest(), byte_size=len(content))
    product = SimpleNamespace(assets=[asset])
    assert main_image_bytes(tmp_path, product, {'excel_main_image': {'asset_id': 'main', 'source_url': 'main'}})
    assert main_image_bytes(tmp_path, product, {'excel_main_image': None}) is None
    assert main_image_bytes(tmp_path, product, {'excel_main_image': {'asset_id': 'other'}}) is None
    assert main_image_bytes(tmp_path, product, {'media': []}) is None
    asset.relative_path = '../outside.png'
    assert main_image_bytes(tmp_path, product, {}) is None


def test_excel_main_image_failure_is_retryable_without_refetching_metadata(tmp_path):
    from app.taobao.parser import TaobaoParser
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, _ = attach(app)
        visits = []
        def collect(task, url):
            visits.append(url)
            return TaobaoParser().parse_product('<h1>端子</h1><meta property="og:image" content="https://img.alicdn.com/main.png">', url)
        collector.collect_product = collect
        store = app.state.collector_runner.asset_store
        real_download = store.download_original
        store.download_original = lambda *args, **kw: (_ for _ in ()).throw(OSError('主图下载失败'))
        tid = client.post('/api/collection/jobs', json={'url': URL, 'collect_images': False}).json()['task_id']
        app.state.worker.run_once()
        failed = client.get(f'/api/collection/jobs/{tid}').json()
        assert failed['task']['status'] == 'PARTIAL'
        assert failed['result']['asset_errors']
        store.download_original = real_download
        assert client.post(f'/api/collection/jobs/{tid}/retry').status_code == 200
        app.state.worker.run_once()
        view = client.get(f'/api/collection/jobs/{tid}').json()
        assert view['task']['status'] == 'SUCCESS'
        assert len(visits) == 1
        with zipfile.ZipFile(io.BytesIO(client.get(f'/api/collection/jobs/{tid}/export.xlsx').content)) as book:
            assert len([n for n in book.namelist() if n.startswith('xl/media/')]) == 1
