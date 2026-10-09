"""Per-product acquisition, download and package ordering without paid traffic."""
import io
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import create_app
from app.collection_service import enqueue_collection
from app.products import CollectionJob
from app.tasks import Task, TaskStatus, utc_now
from app.tasks import TaskDeferred
from app.taobao.collector import TaobaoCollector
from test_scraperapi_collection import fetcher, NoBrowserSession

SHOP = 'https://shop123.taobao.com/category.htm'
PRODUCT = 'https://item.taobao.com/item.htm?id='


def setup(app, folder, events, *, broken=False):
    def handler(request):
        target = request.url.params['url']
        events.append(('request', target, request.url.params.get('render')))
        if 'category.htm' in target:
            if 'page=2' in target:
                with app.state.session_factory() as session:
                    first = session.scalar(select(CollectionJob).where(CollectionJob.source_url == PRODUCT+'123'))
                    assert first.result_data['package_status'] == 'SAVED'
                return httpx.Response(200, text=f'<a href="{PRODUCT}456">商品二</a>')
            return httpx.Response(200, text=f'<a href="{PRODUCT}123">商品一</a><a class="next" href="/category.htm?page=2">下一页</a>')
        if broken:
            return httpx.Response(429, text='limited')
        source_id = target.rsplit('=', 1)[-1]
        return httpx.Response(200, text=f'<h1>商品{source_id}</h1><div id="J_DivItemDesc"><img src="https://img.alicdn.com/{source_id}.png"></div>')
    runner = app.state.collector_runner
    runner.collector = TaobaoCollector(folder, session=NoBrowserSession(), fetcher=fetcher(handler))
    real = runner.asset_store
    stream = io.BytesIO()
    Image.new('RGB', (750, 1200), 'red').save(stream, 'PNG')
    class Store:
        data_dir = real.data_dir
        fail_once = False
        def download_original(self, product_id, url, role, **kwargs):
            events.append(('download', url))
            if self.fail_once:
                self.fail_once = False
                raise OSError('interrupted image transfer')
            return real.save_original_bytes(product_id, url, role, 'image/png', stream.getvalue())
    runner.asset_store = Store()
    return runner.asset_store


def drain(app, limit=35):
    for _ in range(limit):
        if app.state.worker.run_once():
            continue
        with app.state.session_factory.begin() as session:
            pending = list(session.scalars(select(Task).where(Task.status == TaskStatus.PENDING)))
            if not pending:
                return
            for task in pending:
                task.available_at = utc_now()-timedelta(seconds=1)
    pytest.fail('queue did not finish')


def test_shop_collects_downloads_and_saves_before_discovering_next_page(tmp_path):
    app = create_app(tmp_path/'cache', start_worker=False)
    events = []
    output = tmp_path/'中文 资料'; output.mkdir()
    with TestClient(app) as client:
        setup(app, tmp_path/'cache', events)
        tid = client.post('/api/collection/jobs', json={'url': SHOP, 'kind': 'shop',
            'save_directory': str(output), 'collection_mode': 'refresh'}).json()['task_id']
        assert client.get(f'/api/collection/jobs/{tid}').json()['result']['discovery_complete'] is False
        assert app.state.worker.run_once()
        row = client.get(f'/api/collection/jobs/{tid}').json()
        assert len(row['result']['products']) == 1
        assert row['result']['discovery_complete'] is False
        drain(app)
        row = client.get(f'/api/collection/jobs/{tid}').json()
        assert row['task']['status'] == TaskStatus.SUCCESS
        assert row['result']['package_status'] == 'SAVED'
        targets = [e[1] for e in events if e[0] == 'request']
        assert targets == [SHOP, PRODUCT+'123', SHOP+'?page=2', PRODUCT+'456']
        for p in row['result']['products']:
            assert p['scraperapi_requests'] == 1
            assert p['access_method'] == 'scraperapi'
        assert events.index(('download', 'https://img.alicdn.com/123.png')) < events.index(('request', SHOP+'?page=2', None))
        assert all(e[2] == 'true' for e in events if e[0] == 'request' and 'item.htm' in e[1])


def test_saved_discovered_links_drain_first_without_inheriting_shop_login(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    events = []
    with TestClient(app) as client:
        setup(app, tmp_path, events)
        tid = client.post('/api/collection/jobs', json={'url': SHOP, 'kind': 'shop'}).json()['task_id']
        with app.state.session_factory.begin() as session:
            task = session.get(Task, tid); task.status = TaskStatus.CANCELED
            job = session.get(CollectionJob, tid)
            job.result_data = {**job.result_data, 'discovered_urls': [PRODUCT+'123', PRODUCT+'456'],
                'child_task_ids': [], 'discovery_complete': False, 'authentication_required': True,
                'browser_required': True, 'discovery_checkpoint': {'mode': 'direct'}}
        assert client.post(f'/api/collection/jobs/{tid}/resume').status_code == 200
        assert app.state.worker.run_once()  # Drain persisted links before visiting shop again.
        with app.state.session_factory() as session:
            children = session.get(CollectionJob, tid).result_data['child_task_ids']
            assert len(children) == 2
            assert all(not session.get(CollectionJob, c).result_data.get('browser_required') for c in children)
        assert not events
        assert app.state.worker.run_once()
        assert events[0] == ('request', PRODUCT+'123', 'true')
        assert events[1][0] == 'download'


def test_product_download_retry_uses_saved_information_without_second_api_call(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    events = []
    with TestClient(app) as client:
        store = setup(app, tmp_path, events); store.fail_once = True
        tid = client.post('/api/collection/jobs', json={'url': PRODUCT+'123'}).json()['task_id']
        drain(app)
        assert client.get(f'/api/collection/jobs/{tid}').json()['task']['status'] == TaskStatus.PARTIAL
        client.post(f'/api/collection/jobs/{tid}/retry')
        drain(app)
        row = client.get(f'/api/collection/jobs/{tid}').json()
        assert row['task']['status'] == TaskStatus.SUCCESS
        assert row['result']['scraperapi_requests'] == 1
        assert len([e for e in events if e[0] == 'request']) == 1


def test_product_http_failure_does_not_repeat_paid_attempt_or_silently_use_browser(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    events = []
    with TestClient(app) as client:
        setup(app, tmp_path, events, broken=True)
        tid = client.post('/api/collection/jobs', json={'url': PRODUCT+'123'}).json()['task_id']
        drain(app)
        row = client.get(f'/api/collection/jobs/{tid}').json()
        assert row['task']['status'] == TaskStatus.FAILED
        assert len(events) == 1
        assert row['result']['scraperapi_requests'] == 1
        assert '429' in row['task']['error_message']


def test_discovery_checkpoint_does_not_skip_links_if_enqueue_fails(tmp_path, monkeypatch):
    app = create_app(tmp_path, start_worker=False)
    events = []
    with TestClient(app) as client:
        setup(app, tmp_path, events)
        runner = app.state.collector_runner
        enqueue = runner._enqueue_shop_products
        def interrupted(tid, urls, **kwargs):
            if urls:
                raise OSError('interrupted before durable links')
            return enqueue(tid, urls, **kwargs)
        monkeypatch.setattr(runner, '_enqueue_shop_products', interrupted)
        tid = client.post('/api/collection/jobs', json={'url': SHOP, 'kind': 'shop'}).json()['task_id']
        drain(app)
        with app.state.session_factory() as session:
            checkpoint = session.get(CollectionJob, tid).result_data.get('discovery_checkpoint', {})
            assert checkpoint.get('next_url') != SHOP+'?page=2'
            assert not checkpoint.get('complete')


def test_pause_and_restart_resume_product_download_without_new_request(tmp_path):
    output = tmp_path/'目标 目录'; output.mkdir()
    cache = tmp_path/'cache'
    events = []
    app = create_app(cache, start_worker=False)
    with TestClient(app) as client:
        store = setup(app, cache, events)
        def interrupt_download(*args, **kwargs):
            raise TaskDeferred(0)
        store.download_original = interrupt_download
        tid = client.post('/api/collection/jobs', json={'url': SHOP, 'kind': 'shop',
            'save_directory': str(output), 'collection_mode': 'refresh'}).json()['task_id']
        assert app.state.worker.run_once()  # Discover first page.
        assert app.state.worker.run_once()  # Persist first product info, interrupt download.
        with app.state.session_factory() as session:
            child_id = session.get(CollectionJob, tid).result_data['child_task_ids'][0]
            before = session.get(CollectionJob, child_id).preset['save_target']
        assert client.post(f'/api/collection/jobs/{tid}/pause').status_code == 200
    restarted = create_app(cache, start_worker=False)
    with TestClient(restarted) as client:
        setup(restarted, cache, events)
        assert client.get(f'/api/collection/jobs/{tid}').json()['task']['status'] == TaskStatus.CANCELED
        assert not restarted.state.worker.run_once()
        client.post(f'/api/collection/jobs/{tid}/resume')
        drain(restarted)
        row = client.get(f'/api/collection/jobs/{tid}').json()
        assert row['task']['status'] == TaskStatus.SUCCESS
        assert len([e for e in events if e[0] == 'request' and e[1] == PRODUCT+'123']) == 1
        with restarted.state.session_factory() as session:
            child = session.get(CollectionJob, child_id)
            assert child.preset['save_target'] == before
            assert child.result_data['package_status'] == 'SAVED'


def test_connection_test_is_independent_of_last_task_check_and_counters(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    events = []
    with TestClient(app) as client:
        setup(app, tmp_path, events)
        tid = client.post('/api/collection/jobs', json={'url': PRODUCT+'123'}).json()['task_id']
        drain(app)
        network = app.state.collector_runner.collector.fetcher
        network.scraperapi.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'origin': '203.0.113.8'}))
        app.state.collection_network = network
        response = client.post('/api/collection/network/test')
        assert response.status_code == 200
        assert response.json()['success']
        row = client.get(f'/api/collection/jobs/{tid}').json()
        assert row['result']['scraperapi_requests'] == 1


def test_legacy_unacquired_shop_child_uses_api_despite_inherited_browser_flag(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    events = []
    with TestClient(app) as client:
        setup(app, tmp_path, events)
        tid = client.post('/api/collection/jobs', json={'url': SHOP, 'kind': 'shop'}).json()['task_id']
        with app.state.session_factory.begin() as session:
            child = enqueue_collection(session, url=PRODUCT+'123', child=True)
            child_id = child.task_id
            session.get(CollectionJob, child_id).result_data = {
                'browser_required': True, 'child_only': True, 'parent_task_ids': [tid]}
            parent = session.get(CollectionJob, tid)
            parent.result_data = {**parent.result_data, 'discovery_complete': True,
                'discovered_urls': [PRODUCT+'123'], 'child_task_ids': [child_id],
                'authentication_required': True, 'browser_required': True}
        assert app.state.worker.run_once()
        assert app.state.worker.run_once()
        row = client.get(f'/api/collection/jobs/{child_id}').json()
        assert row['task']['status'] == TaskStatus.SUCCESS
        assert row['result']['scraperapi_requests'] == 1
        assert events[0] == ('request', PRODUCT+'123', 'true')
