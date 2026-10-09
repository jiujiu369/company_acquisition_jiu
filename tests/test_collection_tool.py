import io
import json
import zipfile
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.config import ScraperAPISettings
from app.main import create_app
from app.products import CollectionJob, Product, ProductAsset
from app.scraperapi_service import CollectionFetchService, ScraperAPIService
from app.taobao.collector import TaobaoCollector
from app.taobao.parser import TaobaoParser
from app.taobao.session import ManualRequired
from app.tasks import Task, TaskStatus, utc_now

URL = 'https://item.taobao.com/item.htm?id=123'
SHOP = 'https://shop123.taobao.com/category.htm'
HTML = '<h1>测试商品</h1><div id="J_DivItemDesc"><img src="https://img.alicdn.com/a.png"></div>'


def network(handler):
    return CollectionFetchService(ScraperAPIService(
        ScraperAPISettings(api_key='hidden-test-key', enabled=True, max_attempts=1),
        transport=httpx.MockTransport(handler), sleep=lambda _: None))


def test_public_html_first_and_dynamic_shell_switches_to_browser_once(tmp_path):
    requests, visits = [], []
    def handler(req):
        requests.append(req.url.params['url'])
        return httpx.Response(200, text='<h1>商品</h1><script src="/app.js"></script>')
    class Session:
        def open(self, url):
            visits.append(url)
            return HTML, url, []
        def load_product_details(self):
            return HTML, URL, []
    collector = TaobaoCollector(tmp_path, session=Session(), fetcher=network(handler))
    assert collector.collect_product('one', URL).title == '测试商品'
    assert requests == [URL]
    assert visits == [URL]


def test_public_shop_interaction_continues_in_browser(tmp_path):
    calls = []
    class Session:
        def iter_shop_pages(self, url):
            calls.append(url)
            yield HTML + '<a href="https://item.taobao.com/item.htm?id=456">二</a>', url, []
    fetcher = network(lambda req: httpx.Response(200, text='<a href="'+URL+'">一</a><button>加载更多</button>'))
    collector = TaobaoCollector(tmp_path, session=Session(), fetcher=fetcher)
    assert collector.discover_shop('shop', SHOP) == [URL, 'https://item.taobao.com/item.htm?id=456']
    assert calls == [SHOP]


def test_browser_challenge_never_falls_back_again(tmp_path):
    requests, visits = [], []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, text='<input id="fm-login-id">')
    class Session:
        def open(self, url):
            visits.append(url)
            raise ManualRequired('需要登录')
    collector = TaobaoCollector(tmp_path, session=Session(), fetcher=network(handler))
    with pytest.raises(ManualRequired):
        collector.collect_product('login', URL)
    assert len(requests) == len(visits) == 1


class Collector:
    class Session:
        def cookies(self):
            return {}
    session = Session()
    visits = None
    def __init__(self):
        self.visits = []
    def collect_product(self, task_id, url):
        self.visits.append(url)
        return TaobaoParser().parse_product(HTML, url)
    def discover_shop(self, task_id, url):
        return [URL]
    def close(self):
        pass


def attach(app):
    collector = Collector()
    app.state.collector_runner.collector = collector
    real = app.state.collector_runner.asset_store
    output = io.BytesIO()
    Image.new('RGB', (5, 20), 'blue').save(output, 'PNG')
    class Store:
        data_dir = real.data_dir
        def download_original(self, product, url, role, **kwargs):
            return real.save_original_bytes(product, url, role, 'image/png', output.getvalue())
    app.state.collector_runner.asset_store = Store()
    return collector, output.getvalue()


def test_only_collection_ui_and_mixed_batch_reuses_success(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, content = attach(app)
        homepage = client.get('/')
        assert 'source-urls' in homepage.text
        for route in ('/media', '/ai', '/reviews', '/trash', '/tasks', '/api/ai/providers', '/api/media/assets'):
            assert client.get(route).status_code == 404
        result = client.post('/api/collection/jobs/batch', json={'urls':[URL, SHOP, URL+' &bad']})
        # URL validation is atomic, so invalid input must not partially enqueue.
        assert result.status_code == 422
        assert client.get('/api/collection/jobs').json()['total'] == 0
        result = client.post('/api/collection/jobs/batch', json={'urls':[URL, SHOP]})
        assert result.status_code == 201
        ids = result.json()['task_ids']
        for _ in range(8):
            app.state.worker.run_once()
            with app.state.session_factory.begin() as session:
                for task in session.scalars(select(Task).where(Task.status == TaskStatus.PENDING)):
                    task.available_at = None
        assert collector.visits == [URL]
        again = client.post('/api/collection/jobs', json={'url':URL}).json()
        assert again['task_id'] == ids[0] and again['status'] == 'SUCCESS'
        archive = client.get(f'/api/collection/jobs/{ids[1]}/export.zip')
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            images = [n for n in z.namelist() if n.startswith('assets/') or '/assets/' in n]
            assert len(images) == 1 and z.read(images[0]) == content


def test_product_pause_and_restart_do_not_claim_retired_history(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        task_id = client.post('/api/collection/jobs', json={'url':URL}).json()['task_id']
        assert client.post(f'/api/collection/jobs/{task_id}/pause').status_code == 200
        assert not app.state.worker.run_once()
        assert client.post(f'/api/collection/jobs/{task_id}/resume').status_code == 200
        with app.state.session_factory.begin() as session:
            session.get(Task, task_id).status = TaskStatus.RUNNING
            session.get(Task, task_id).lease_until = utc_now()+timedelta(minutes=10)
            session.add(Task(task_id='retired',task_type='ai.image',status=TaskStatus.PENDING,created_at=utc_now()))
    restarted = create_app(tmp_path, start_worker=False)
    with TestClient(restarted) as client:
        attach(restarted)
        assert restarted.state.worker.run_once()
        assert not restarted.state.worker.run_once()
        assert client.get(f'/api/collection/jobs/{task_id}').json()['task']['status'] == TaskStatus.SUCCESS
        with restarted.state.session_factory() as session:
            assert session.get(Task, 'retired').status == TaskStatus.PENDING


def test_repairing_missing_original_does_not_revisit_completed_page(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, content = attach(app)
        tid = client.post('/api/collection/jobs', json={'url':URL}).json()['task_id']
        app.state.worker.run_once()
        with app.state.session_factory() as session:
            asset = session.scalar(select(ProductAsset))
            original = tmp_path / asset.relative_path
            original.write_bytes(b'corrupted')
        assert client.post('/api/collection/jobs', json={'url':URL}).json()['status'] == 'PENDING'
        assert app.state.worker.run_once()
        assert collector.visits == [URL]
        assert client.post('/api/collection/jobs', json={'url':URL}).json()['status'] == 'SUCCESS'
        assert original.read_bytes() == b'corrupted'  # Existing files are never replaced, even if damaged.
        with app.state.session_factory() as session:
            good = [a for a in session.scalars(select(ProductAsset)) if (tmp_path/a.relative_path).read_bytes() == content]
            assert len(good) == 1


def test_missing_images_retry_reacquires_page_instead_of_reusing_empty_capture(tmp_path):
    app = create_app(tmp_path,start_worker=False)
    with TestClient(app) as client:
        collector,_ = attach(app)
        original = collector.collect_product
        collector.collect_product = lambda task_id,url: TaobaoParser().parse_product('<h1>商品尚未加载</h1>',url)
        tid = client.post('/api/collection/jobs',json={'url':URL}).json()['task_id']
        app.state.worker.run_once()
        assert client.get(f'/api/collection/jobs/{tid}').json()['task']['status'] == 'PARTIAL'
        collector.collect_product = original
        client.post(f'/api/collection/jobs/{tid}/retry')
        app.state.worker.run_once()
        assert client.get(f'/api/collection/jobs/{tid}').json()['task']['status'] == 'SUCCESS'


def test_original_product_gallery_excludes_recommendations_and_resize_suffixes():
    html = '''<h1>真实商品</h1><div class="recommend"><div class="tb-gallery"><img src="https://img.alicdn.com/ad.jpg"></div></div>
      <div class="tb-gallery"><img src="https://img.alicdn.com/small.jpg" data-original="https://img.alicdn.com/real.jpg_300x300.jpg"></div>
      <script>window.__INITIAL_STATE__={"recommend":{"images":["https://img.alicdn.com/another-ad.jpg"]}};</script>'''
    parsed = TaobaoParser().parse_product(html,URL)
    assert parsed.images == [{'url':'https://img.alicdn.com/real.jpg','role':'main'}]


def test_batch_deduplicates_product_id_across_taobao_and_tmall(tmp_path):
    app = create_app(tmp_path,start_worker=False)
    with TestClient(app) as client:
        collector,_ = attach(app)
        created = client.post('/api/collection/jobs/batch',json={'urls':[URL,'https://detail.tmall.com/item.htm?id=123']})
        assert created.json()['count'] == 1
        app.state.worker.run_once()
        assert collector.visits == [URL]


def test_valid_success_is_reused_even_with_a_newer_failed_historical_task(tmp_path):
    app = create_app(tmp_path,start_worker=False)
    with TestClient(app) as client:
        attach(app)
        tid = client.post('/api/collection/jobs',json={'url':URL}).json()['task_id']
        app.state.worker.run_once()
        with app.state.session_factory.begin() as session:
            session.add(Task(task_id='older-failure',task_type='taobao.product',status=TaskStatus.FAILED,created_at=utc_now()))
            session.flush()
            session.add(CollectionJob(task_id='older-failure',kind='product',source_url=URL,preset={},result_data={}))
        again = client.post('/api/collection/jobs',json={'url':URL}).json()
        assert again['status'] == 'SUCCESS' and again['task_id'] == tid
