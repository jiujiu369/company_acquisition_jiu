import io
import zipfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.products import CollectionJob
from app.taobao.collector import TaobaoCollector
from test_collection_tool import attach, network, URL, SHOP


HTML = '<h1>接线端子</h1><script>window.__INITIAL_STATE__={"skus":[{"skuId":"1","name":"JG（20只）","price":"21.7"}]};</script>'


def test_excel_only_skips_download_and_saves_xlsx_successfully(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        runner = app.state.collector_runner
        calls = []
        runner.asset_store.download_original = lambda *a, **kw: calls.append(a)
        task = client.post('/api/collection/jobs', json={'url': URL, 'collect_images': False}).json()['task_id']
        app.state.worker.run_once()
        view = client.get(f'/api/collection/jobs/{task}').json()
        assert view['task']['status'] == 'SUCCESS', view
        assert not calls and view['result']['collect_images'] is False
        folder = Path(view['result']['package_path'])
        assert (folder / '测试商品.xlsx').is_file()
        assert not (folder / 'assets').exists()
        response = client.get(f'/api/collection/jobs/{task}/export.xlsx')
        assert response.status_code == 200 and response.content.startswith(b'PK')
        assert 'spreadsheetml' in response.headers['content-type']
        assert client.get(f'/api/collection/jobs/{task}/package-file/测试商品.xlsx').content.startswith(b'PK')
        assert client.get(f"/api/collection/products/{view['result']['product_id']}/export.xlsx").content.startswith(b'PK')
        assert client.post('/api/collection/export.xlsx', json={'task_ids': [task]}).content.startswith(b'PK')
        response = client.get(f'/api/collection/jobs/{task}/export.zip')
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert '测试商品.xlsx' in archive.namelist()
            assert not any(n.startswith('assets/') for n in archive.namelist())
        again = client.post('/api/collection/jobs', json={'url': URL, 'collect_images': False}).json()
        assert again['task_id'] == task and again['status'] == 'SUCCESS'
        other = client.post('/api/collection/jobs', json={'url': URL, 'collect_images': True}).json()
        assert other['task_id'] != task


def test_excel_only_api_page_with_skus_does_not_need_detail_images(tmp_path):
    class Session:
        def open(self, *_):
            raise AssertionError('Complete metadata does not need browser detail loading')
        def close(self):
            pass
    collector = TaobaoCollector(tmp_path, session=Session(),
        fetcher=network(lambda _: httpx.Response(200, text=HTML)))
    collector.collect_images = False
    result = collector.collect_product('task', URL)
    assert result.skus[0]['package_quantity'] == 20


def test_excel_only_direct_browser_does_not_scroll_details(tmp_path):
    class Session:
        def open(self, *_):
            return HTML, URL, []
        def load_product_details(self):
            raise AssertionError('Excel-only must not load description images')
        def close(self):
            pass
    collector = TaobaoCollector(tmp_path, session=Session())
    collector.collect_images = False
    assert collector.collect_product('task', URL).skus[0]['package_price'] == '21.70'


def test_shop_children_keep_excel_only_choice(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        runner = app.state.collector_runner
        calls = []
        runner.asset_store.download_original = lambda *a, **kw: calls.append(a)
        task = client.post('/api/collection/jobs', json={'url': SHOP, 'collect_images': False}).json()['task_id']
        for _ in range(6):
            app.state.worker.run_once()
        with app.state.session_factory() as session:
            parent = session.get(CollectionJob, task)
            assert parent.result_data['child_task_ids']
            for cid in parent.result_data['child_task_ids']:
                assert session.get(CollectionJob, cid).preset['collect_images'] is False
        assert not calls


def test_excel_only_reuse_does_not_copy_previous_images(tmp_path):
    from test_preparation_batches import submit
    app = create_app(tmp_path / 'data', start_worker=False)
    with TestClient(app) as client:
        collector, _ = attach(app)
        first = submit(client, tmp_path / 'images', mode='refresh')
        app.state.worker.run_once()
        original = client.get(f'/api/collection/jobs/{first}').json()['result']
        assert original['total_asset_count'] > 0
        target = tmp_path / 'excel'; target.mkdir()
        second = client.post('/api/collection/jobs/batch', json={'urls': [URL],
            'save_directory': str(target), 'collection_mode': 'reuse', 'collect_images': False}).json()['task_ids'][0]
        app.state.worker.run_once()
        result = client.get(f'/api/collection/jobs/{second}').json()['result']
        assert result['reused'] and len(collector.visits) == 1
        assert result['total_asset_count'] == 0 and result['package_status'] == 'SAVED'
        assert not (Path(result['package_path']) / 'assets').exists()
        assert (Path(original['package_path']) / 'assets').is_dir()
