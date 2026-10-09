import io
import pytest
from datetime import timedelta

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.main import create_app
from app.products import ProductAsset
from app.taobao.parser import TaobaoParser
from app.taobao.collector import TaobaoCollector
from app.taobao.session import ManualRequired, TaobaoSession
from app.tasks import Task, TaskStatus, utc_now


SHOP = "https://shop115331568.taobao.com/category.htm"
DETAIL_HTML = '''<h1>详情测试商品</h1><img src="//img.alicdn.com/logo.png">
<div class="tb-gallery"><img src="//img.alicdn.com/main.jpg"></div>
<div id="J_DivItemDesc"><img src="//img.alicdn.com/a.jpg_100x100.jpg" data-original="//img.alicdn.com/a.jpg">
<img data-lazy-src="//img.alicdn.com/b.png">
<img srcset="//img.alicdn.com/c-small.jpg 200w, //img.alicdn.com/c.jpg 750w">
<div style="background-image:url(//img.alicdn.com/d.png)"></div>
<div class="recommend"><img src="//img.alicdn.com/recommend.jpg"></div>
<div class="price-explanation"><img src="//img.alicdn.com/rules.jpg"></div>
<img data-src="//img.alicdn.com/a.jpg"></div>'''


def test_details_keep_order_and_exclude_unrelated_media():
    parsed = TaobaoParser().parse_product(DETAIL_HTML, "https://item.taobao.com/item.htm?id=1", detail_only=True)
    assert [item["url"] for item in parsed.images] == [
        "https://img.alicdn.com/a.jpg", "https://img.alicdn.com/b.png",
        "https://img.alicdn.com/c.jpg", "https://img.alicdn.com/d.png"]
    assert all(item["role"] == "detail" for item in parsed.images)
    assert parsed.videos == []


def test_missing_details_never_falls_back_to_gallery_or_json():
    html = '<h1>商品</h1><script>window.__INITIAL_STATE__={"recommend":{"images":["//img.alicdn.com/ad.jpg"]}};</script><img src="//img.alicdn.com/main.jpg">'
    parsed = TaobaoParser().parse_product(html, "https://item.taobao.com/item.htm?id=1", detail_only=True)
    assert parsed.images == []


def test_detail_like_elements_inside_recommendations_are_excluded():
    html = '<h1>商品</h1><div id="J_DivItemDesc"><img src="//img.alicdn.com/real.jpg"></div><div class="recommend"><div class="descV8-singleImage"><img src="//img.alicdn.com/ad.jpg"></div></div>'
    parsed = TaobaoParser().parse_product(html, 'https://item.taobao.com/item.htm?id=1', detail_only=True)
    assert [item['url'] for item in parsed.images] == ['https://img.alicdn.com/real.jpg']


def test_shop_discovery_ignores_cart_and_recommendation_responses():
    network = [
        {"request_url": "https://api.taobao.com/cart", "data": {"items": [{"itemId": 999}]}},
        {"request_url": "https://api.taobao.com/shop", "data": {"items": [{"itemId": 1}], "recommendItems": [{"itemId": 888}]}}]
    assert TaobaoParser().discover_products('<h1>店铺</h1>', SHOP, network) == ['https://item.taobao.com/item.htm?id=1']


def test_shop_discovery_does_not_truncate_after_200_products():
    html = ''.join(f'<a href="https://item.taobao.com/item.htm?id={i}">商品</a>' for i in range(1, 306))
    assert len(TaobaoParser().discover_products(html, SHOP)) == 305


class BatchCollector:
    class Session:
        def cookies(self):
            return {}
    session = Session()

    def __init__(self):
        self.fail = {"2"}
        self.manual = False

    def discover_shop(self, task_id, url):
        return [f"https://item.taobao.com/item.htm?id={i}" for i in (1, 2, 3, 1)]

    def collect_product(self, task_id, url):
        if self.manual:
            raise ManualRequired("请人工登录")
        if url.rsplit("=", 1)[-1] in self.fail:
            raise TimeoutError("商品页面打不开")
        return TaobaoParser().parse_product('<h1>长图商品</h1><div id="J_DivItemDesc"><img src="https://img.alicdn.com/long.png"></div>', url)

    def close(self):
        pass


class LongImageStore:
    def __init__(self, real):
        self.real, self.data_dir = real, real.data_dir
        stream = io.BytesIO()
        Image.new("RGB", (750, 5000), "red").save(stream, format="PNG")
        self.content = stream.getvalue()

    def download_original(self, product_id, source_url, role, **kwargs):
        return self.real.save_original_bytes(product_id, source_url, role, "image/png", self.content)


def drain(app, limit=25):
    for _ in range(limit):
        if app.state.worker.run_once():
            continue
        with app.state.session_factory.begin() as session:
            pending = session.scalars(select(Task).where(Task.status == TaskStatus.PENDING)).all()
            if not pending:
                break
            for task in pending:
                task.available_at = utc_now() - timedelta(seconds=1)


def setup_runner(app):
    collector = BatchCollector()
    store = LongImageStore(app.state.collector_runner.asset_store)
    app.state.collector_runner.collector = collector
    app.state.collector_runner.asset_store = store
    return collector, store


def test_shop_automatically_collects_all_products_and_retry_preserves_success(tmp_path):
    app = create_app(data_dir=tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, store = setup_runner(app)
        parent = client.post('/api/collection/jobs', json={"url": SHOP, "kind": "shop"}).json()["task_id"]
        drain(app)
        job = client.get(f'/api/collection/jobs/{parent}').json()
        assert job['task']['status'] == TaskStatus.PARTIAL
        assert job['result']['total_products'] == 3
        assert job['result']['completed_products'] == 2
        assert job['result']['failed_products'] == 1
        assert job['result']['pending_products'] == 0
        with app.state.session_factory() as session:
            before = {a.id for a in session.scalars(select(ProductAsset))}
            assert len(before) == 2
            for asset in session.scalars(select(ProductAsset)):
                assert (asset.width, asset.height, asset.role) == (750, 5000, 'detail')
                assert (tmp_path / asset.relative_path).read_bytes() == store.content
        collector.fail.clear()
        assert client.post(f'/api/collection/jobs/{parent}/retry').status_code == 200
        drain(app)
        job = client.get(f'/api/collection/jobs/{parent}').json()
        assert job['task']['status'] == TaskStatus.SUCCESS
        assert job['result']['completed_products'] == 3
        with app.state.session_factory() as session:
            after = {a.id for a in session.scalars(select(ProductAsset))}
            assert before < after
            assert len(after) == 3


def test_shop_pause_resume_and_manual_verification(tmp_path):
    app = create_app(data_dir=tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, _ = setup_runner(app)
        collector.fail.clear()
        parent = client.post('/api/collection/jobs', json={"url": SHOP, "kind": "shop"}).json()["task_id"]
        assert app.state.worker.run_once()  # Discover and persist children.
        assert client.post(f'/api/collection/jobs/{parent}/pause').status_code == 200
        drain(app, 4)
        with app.state.session_factory() as session:
            assert not session.scalars(select(ProductAsset)).all()
        collector.manual = True
        assert client.post(f'/api/collection/jobs/{parent}/resume').status_code == 200
        drain(app, 8)
        assert client.get(f'/api/collection/jobs/{parent}').json()['task']['status'] == TaskStatus.NEEDS_MANUAL
        collector.manual = False
        assert client.post(f'/api/collection/jobs/{parent}/resume').status_code == 200
        drain(app)
        assert client.get(f'/api/collection/jobs/{parent}').json()['result']['completed_products'] == 3


def test_pending_shop_products_survive_application_restart(tmp_path):
    app = create_app(data_dir=tmp_path, start_worker=False)
    with TestClient(app) as client:
        setup_runner(app)
        parent = client.post('/api/collection/jobs', json={"url": SHOP, "kind": "shop"}).json()['task_id']
        assert app.state.worker.run_once()
        assert app.state.worker.run_once()  # First product completes.
        before = client.get(f'/api/collection/jobs/{parent}').json()['result']['products'][0]['product_id']
    restarted = create_app(data_dir=tmp_path, start_worker=False)
    with TestClient(restarted) as client:
        collector, _ = setup_runner(restarted)
        collector.fail.clear()
        drain(restarted)
        result = client.get(f'/api/collection/jobs/{parent}').json()['result']
        assert result['completed_products'] == 3
        assert result['products'][0]['product_id'] == before
        with restarted.state.session_factory() as session:
            assert len(session.scalars(select(ProductAsset)).all()) == 3


def test_discovery_resume_preserves_products_from_earlier_pages(tmp_path):
    class PageSession:
        ready = False
        def iter_shop_pages(self, url):
            if not self.ready:
                yield '<a href="https://item.taobao.com/item.htm?id=1">一</a>', url, []
                raise ManualRequired('翻页需要人工验证')
            yield '<a href="https://item.taobao.com/item.htm?id=2">二</a>', url, []
        def close(self):
            pass
        def open(self, url):
            self.product_url = url
        def load_product_details(self):
            return '<h1>商品</h1><div id="J_DivItemDesc"><img src="https://img.alicdn.com/long.png"></div>', self.product_url, []
        def cookies(self):
            return {}
    app = create_app(data_dir=tmp_path, start_worker=False)
    with TestClient(app) as client:
        page_session = PageSession()
        app.state.collector_runner.collector = TaobaoCollector(tmp_path, session=page_session)
        app.state.collector_runner.asset_store = LongImageStore(app.state.collector_runner.asset_store)
        parent = client.post('/api/collection/jobs', json={"url": SHOP, "kind": "shop"}).json()['task_id']
        assert app.state.worker.run_once()
        assert client.get(f'/api/collection/jobs/{parent}').json()['result']['count'] == 1
        assert app.state.worker.run_once()  # Download the first product before resuming discovery.
        with app.state.session_factory.begin() as session:
            session.get(Task, parent).available_at = utc_now()-timedelta(seconds=1)
        assert app.state.worker.run_once()  # Discovery now reaches the verification.
        assert client.get(f'/api/collection/jobs/{parent}').json()['task']['status'] == TaskStatus.NEEDS_MANUAL
        page_session.ready = True
        assert client.post(f'/api/collection/jobs/{parent}/resume').status_code == 200
        assert app.state.worker.run_once()
        result = client.get(f'/api/collection/jobs/{parent}').json()['result']
        assert result['discovered_urls'] == ['https://item.taobao.com/item.htm?id=1', 'https://item.taobao.com/item.htm?id=2']
        assert len(result['child_task_ids']) == 2


def test_browser_shop_pagination_load_more_and_detail_lazy_loading(tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel='chrome')
        context = browser.new_context(viewport={"width": 1200, "height": 800})
        page = context.new_page()
        first = '''<h1>测试店铺</h1><a href="https://item.taobao.com/item.htm?id=1">一</a>
          <button onclick="this.insertAdjacentHTML('beforebegin', '<a href=https://item.taobao.com/item.htm?id=2>二</a>');this.remove()">加载更多</button>
          <button onclick="location.href='http://fixture.test/catalog?page=2'">下一页</button>'''
        second = '''<h1>第二页</h1><div style="height:9000px">商品区域</div><a href="https://item.taobao.com/item.htm?id=3">三</a>
          <script>let scheduled=false;window.addEventListener('scroll',()=>{if(!scheduled&&scrollY+innerHeight>=document.documentElement.scrollHeight-2){scheduled=true;setTimeout(()=>document.body.insertAdjacentHTML('beforeend','<a href=https://item.taobao.com/item.htm?id=4>四</a>'),1800);}});</script>'''
        detail = '''<h1>商品</h1><div style="height:1500px"></div><div id="J_DivItemDesc" style="height:1800px"><img id="lazy"></div>
          <script>let scheduled=false;window.addEventListener('scroll', () => {
          if(scrollY>900)document.querySelector('#lazy').setAttribute('data-src','https://img.alicdn.com/lazy.png');
          if(!scheduled&&scrollY+innerHeight>=document.documentElement.scrollHeight-2){scheduled=true;setTimeout(()=>document.querySelector('#J_DivItemDesc').insertAdjacentHTML('beforeend','<img data-src=https://img.alicdn.com/late.png>'),1800);}});</script>'''
        def route(request):
            url = request.request.url
            if '/catalog' in url:
                body = second if 'page=2' in url else first
            elif '/framed' in url:
                body = '<h1>iframe 商品</h1><iframe width="800" height="400" src="http://fixture.test/frame"></iframe><div class="recommend"><iframe src="http://fixture.test/ad"></iframe></div>'
            elif '/ad' in url:
                body = '<div class="descV8-singleImage"><img data-src="https://img.alicdn.com/ad.png"></div>'
            elif '/frame' in url:
                body = '''<div id="J_DivItemDesc" style="height:1800px"><img id="frame-lazy"></div>
                    <script>window.addEventListener('scroll',()=>{if(scrollY>900)document.querySelector('#frame-lazy').setAttribute('data-src','https://img.alicdn.com/frame.png');});</script><div class="recommend"><div class="descV8-singleImage"><img data-src="https://img.alicdn.com/ad.png"></div></div>'''
            elif '/zero' in url:
                body = '<h1>零高度详情</h1><div id="J_DivItemDesc" style="height:0"><img data-src="https://img.alicdn.com/zero.png"></div>'
            else:
                body = detail
            request.fulfill(status=200, content_type='text/html; charset=utf-8', body=body)
        page.route('**/*', route)
        session = TaobaoSession(tmp_path)
        session.context, session.page = context, page
        snapshots = list(session.iter_shop_pages('http://fixture.test/catalog'))
        assert {url for _, url, _ in snapshots} == {'http://fixture.test/catalog', 'http://fixture.test/catalog?page=2'}
        parser = TaobaoParser()
        urls = list(dict.fromkeys(url for html, _, network in snapshots for url in parser.discover_products(html, SHOP, network)))
        assert urls == ["https://item.taobao.com/item.htm?id=1", "https://item.taobao.com/item.htm?id=2", "https://item.taobao.com/item.htm?id=3", "https://item.taobao.com/item.htm?id=4"]
        session.open('http://fixture.test/product')
        html, _, _ = session.load_product_details()
        parsed = parser.parse_product(html, 'https://item.taobao.com/item.htm?id=1', detail_only=True)
        assert [item['url'] for item in parsed.images] == ['https://img.alicdn.com/lazy.png', 'https://img.alicdn.com/late.png']
        session.open('http://fixture.test/framed')
        html, _, _ = session.load_product_details()
        parsed = parser.parse_product(html, 'https://item.taobao.com/item.htm?id=1', detail_only=True)
        assert [item['url'] for item in parsed.images] == ['https://img.alicdn.com/frame.png']
        session.open('http://fixture.test/zero')
        html, _, _ = session.load_product_details()
        parsed = parser.parse_product(html, 'https://item.taobao.com/item.htm?id=1', detail_only=True)
        assert [item['url'] for item in parsed.images] == ['https://img.alicdn.com/zero.png']
        with pytest.raises(ManualRequired, match='全部商品'):
            list(session.iter_shop_pages('http://fixture.test/catalog?q=blue'))
        context.close()
        browser.close()
