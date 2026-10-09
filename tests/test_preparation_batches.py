import json
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import create_app
from app.products import Product, ProductCapture
from test_collection_tool import attach, URL, SHOP


def submit(client, target, mode='reuse', url=URL):
    target.mkdir(exist_ok=True)
    r = client.post('/api/collection/jobs/batch', json={'urls': [url], 'save_directory': str(target), 'collection_mode': mode})
    assert r.status_code == 201, r.text
    return r.json()['task_ids'][0]


def test_different_targets_reuse_bytes_but_not_task(tmp_path):
    app = create_app(tmp_path / 'data', start_worker=False)
    with TestClient(app) as c:
        collector, content = attach(app)
        first = submit(c, tmp_path / '中文 甲'); app.state.worker.run_once()
        second = submit(c, tmp_path / '中文 乙'); app.state.worker.run_once()
        assert first != second and len(collector.visits) == 1
        for tid in (first, second):
            r = c.get(f'/api/collection/jobs/{tid}').json()['result']
            assert r['package_status'] == 'SAVED'
            assert Path(r['package_path']).joinpath('skus.csv').exists()
        assert c.get(f'/api/collection/jobs/{first}').json()['result']['package_path'] != c.get(f'/api/collection/jobs/{second}').json()['result']['package_path']


def test_manual_survives_refresh_and_keeps_history(tmp_path):
    app = create_app(tmp_path / 'data', start_worker=False)
    with TestClient(app) as c:
        collector, _ = attach(app)
        first = submit(c, tmp_path / '目标'); app.state.worker.run_once()
        pid = c.get(f'/api/collection/jobs/{first}').json()['result']['product_id']
        old = c.get(f'/api/collection/jobs/{first}').json()['result']['package_path']
        change = c.post(f'/api/collection/products/{pid}/manual', json={'overrides': {'fields.title': {'value':'人工标题','status':'obtained'},
             'fields.stock': {'value':0,'status':'obtained'}}})
        assert change.status_code == 200, change.text
        second = submit(c, tmp_path / '目标', 'refresh'); app.state.worker.run_once()
        d = c.get(f'/api/collection/products/{pid}/preparation').json()['document']
        assert d['effective']['fields']['title']['value'] == '人工标题'
        assert d['effective']['fields']['stock']['value'] == 0
        assert d['collected']['fields']['title']['value'] != '人工标题'
        assert len(collector.visits) == 2 and Path(old).is_dir()
        with app.state.session_factory() as s:
            assert len(s.scalars(select(ProductCapture)).all()) == 2


def test_invalid_directory_does_not_enqueue(tmp_path):
    app = create_app(tmp_path / 'data', start_worker=False)
    with TestClient(app) as c:
        f = tmp_path / '不是目录'; f.write_text('x')
        r = c.post('/api/collection/jobs/batch', json={'urls': [URL], 'save_directory': str(f)})
        assert r.status_code == 422
        assert c.get('/api/collection/jobs').json()['total'] == 0


def test_save_failure_preserves_collection_and_can_resave(tmp_path, monkeypatch):
    app = create_app(tmp_path / 'data', start_worker=False)
    with TestClient(app) as c:
        collector, _ = attach(app)
        import app.preparation_service as service
        original = service.write_package
        monkeypatch.setattr(service, 'write_package', lambda *a, **k: {'status':'FAILED','path': str(tmp_path), 'errors':['磁盘不可写']})
        tid = submit(c, tmp_path / '目标'); app.state.worker.run_once()
        r = c.get(f'/api/collection/jobs/{tid}').json()
        assert r['task']['status'] == 'SUCCESS' and r['result']['package_status'] == 'FAILED'
        monkeypatch.setattr(service, 'write_package', original)
        new = tmp_path / '补存'; new.mkdir()
        saved = c.post(f'/api/collection/jobs/{tid}/save-package', json={'save_directory':str(new)})
        assert saved.status_code == 200 and saved.json()['status'] == 'SAVED'
        assert len(collector.visits) == 1


def test_pause_restart_uses_original_batch_directory(tmp_path):
    data = tmp_path / 'data'
    app = create_app(data, start_worker=False)
    with TestClient(app) as c:
        tid = submit(c, tmp_path / '原目录')
        c.post(f'/api/collection/jobs/{tid}/pause')
    restarted = create_app(data, start_worker=False)
    with TestClient(restarted) as c:
        attach(restarted)
        assert c.post(f'/api/collection/jobs/{tid}/resume').status_code == 200
        restarted.state.worker.run_once()
        r = c.get(f'/api/collection/jobs/{tid}').json()['result']
        assert str(tmp_path / '原目录') in r['package_path'] and r['package_status'] == 'SAVED'


def test_interrupted_completed_save_is_recovered_without_source_read(tmp_path):
    from app.products import CollectionJob, PackageSave
    data = tmp_path / 'data'
    app = create_app(data, start_worker=False)
    with TestClient(app) as c:
        attach(app)
        tid = submit(c, tmp_path / '目标'); app.state.worker.run_once()
        with app.state.session_factory.begin() as s:
            j = s.get(CollectionJob, tid)
            j.result_data = {**j.result_data, 'package_status':'SAVING'}
            s.add(PackageSave(task_id=tid, product_id=j.result_data['product_id'],
                capture_id=j.result_data['preparation_capture_id'], target_path=j.preset['save_target'], status='SAVING'))
    restarted = create_app(data, start_worker=False)
    with TestClient(restarted) as c:
        r = c.get(f'/api/collection/jobs/{tid}').json()
        assert r['task']['status'] == 'SUCCESS'
        assert r['result']['package_status'] == 'SAVED'
        with restarted.state.session_factory() as s:
            assert not s.scalars(select(PackageSave).where(PackageSave.status=='SAVING')).all()


def test_shop_children_inherit_target_and_retarget_pending_children(tmp_path):
    from app.products import CollectionJob
    app = create_app(tmp_path / 'data', start_worker=False)
    with TestClient(app) as c:
        attach(app)
        tid = submit(c, tmp_path / '原目录', url=SHOP)
        app.state.worker.run_once()
        with app.state.session_factory() as s:
            j=s.get(CollectionJob,tid); children=j.result_data['child_task_ids']
            assert children
            assert all(s.get(CollectionJob,k).preset['save_target']==j.preset['save_target'] for k in children)
        app.state.worker.run_once()
        target=tmp_path/'新目录';target.mkdir()
        r=c.post(f'/api/collection/jobs/{tid}/save-package',json={'save_directory':str(target)})
        assert r.status_code==200,r.text
        with app.state.session_factory() as s:
            assert all(s.get(CollectionJob,k).preset['save_target']==str(target) for k in children)


def test_json_download_uses_final_structured_values(tmp_path):
    app=create_app(tmp_path/'data',start_worker=False)
    with TestClient(app) as c:
        attach(app);tid=submit(c,tmp_path/'目标');app.state.worker.run_once()
        pid=c.get(f'/api/collection/jobs/{tid}').json()['result']['product_id']
        assert c.post(f'/api/collection/products/{pid}/manual',json={'overrides':{'fields.title':{'value':'修订后商品名称','status':'obtained'}}}).status_code==200
        d=c.get(f'/api/collection/jobs/{tid}/product.json').json()
        assert d['fields']['title']=='修订后商品名称'
        assert 'stock' not in d['fields']
        assert 'collected' not in d
        preparation=c.get(f'/api/collection/products/{pid}').json()['preparation']
        assert preparation['collected']['fields']['stock']['value'] is None


def test_shutdown_during_copy_defers_task_and_restart_saves(tmp_path, monkeypatch):
    from app.tasks import TaskDeferred
    import app.preparation_service as service
    original=service.write_package;data=tmp_path/'data'
    app=create_app(data,start_worker=False)
    with TestClient(app) as c:
        collector,_=attach(app);tid=submit(c,tmp_path/'目标')
        def interrupted(*args,**kwargs): raise TaskDeferred(0)
        monkeypatch.setattr(service,'write_package',interrupted)
        app.state.worker.run_once()
        r=c.get(f'/api/collection/jobs/{tid}').json()
        assert r['task']['status']=='PENDING' and r['result']['package_status']=='PENDING'
    monkeypatch.setattr(service,'write_package',original)
    restarted=create_app(data,start_worker=False)
    with TestClient(restarted) as c:
        collector,_=attach(restarted);restarted.state.worker.run_once()
        r=c.get(f'/api/collection/jobs/{tid}').json()
        assert r['result']['package_status']=='SAVED' and not collector.visits


def test_previous_scraperapi_is_used_with_selected_directory(tmp_path):
    import httpx
    from app.taobao.collector import TaobaoCollector
    from test_scraperapi_collection import fetcher, NoBrowserSession, DETAIL
    app=create_app(tmp_path/'data',start_worker=False)
    with TestClient(app) as c:
        _,_=attach(app);runner=app.state.collector_runner
        calls=[]
        def source(request):
            calls.append(str(request.url));return httpx.Response(200,text=DETAIL)
        runner.collector=TaobaoCollector(tmp_path/'data',session=NoBrowserSession(),fetcher=fetcher(source))
        tid=submit(c,tmp_path/'ScraperAPI 中文');app.state.worker.run_once()
        r=c.get(f'/api/collection/jobs/{tid}').json()
        assert r['task']['status']=='SUCCESS' and r['result']['access_method']=='scraperapi'
        assert r['result']['package_status']=='SAVED' and len(calls)==1


def test_unwritable_directory_and_cross_batch_pause_are_isolated(tmp_path,monkeypatch):
    import app.preparation_service as service
    from app.products import CollectionJob
    app=create_app(tmp_path/'data',start_worker=False)
    with TestClient(app) as c:
        attach(app)
        first=submit(c,tmp_path/'甲');second=submit(c,tmp_path/'乙')
        assert c.post(f'/api/collection/jobs/{first}/pause').status_code==200
        app.state.worker.run_once()
        assert c.get(f'/api/collection/jobs/{second}').json()['result']['package_status']=='SAVED'
        def deny(*args,**kwargs):raise PermissionError('test deny')
        monkeypatch.setattr(service.tempfile,'NamedTemporaryFile',deny)
        result=c.post('/api/collection/jobs/batch',json={'urls':[URL],'save_directory':str(tmp_path)})
        assert result.status_code==422 and c.get('/api/collection/jobs').json()['total']==2


def test_saved_seller_values_survive_public_refresh_and_orphan_manual_is_readable(tmp_path):
    from app.taobao.parser import ParsedProduct
    from test_seller_read import record
    app=create_app(tmp_path/'data',start_worker=False)
    with TestClient(app) as c:
        collector,_=attach(app)
        collector.collect_product=lambda task_id,url:ParsedProduct('123',URL,'商品',skus=[{'source_sku_id':'old','properties':{'颜色':'旧'}}],images=[{'url':'https://img.alicdn.com/a.png','role':'main'}])
        tid=submit(c,tmp_path/'目标');app.state.worker.run_once()
        pid=c.get(f'/api/collection/jobs/{tid}').json()['result']['product_id']
        source=record();source['skus']=[{'source_sku_id':'old','stock':0}]
        assert c.post(f'/api/collection/products/{pid}/saved-record',json={'source':source}).status_code==200
        doc=c.get(f'/api/collection/products/{pid}/preparation').json()['document']
        key=doc['collected']['skus'][0]['local_sku_id']
        assert c.post(f'/api/collection/products/{pid}/manual',json={'overrides':{f'skus.{key}.stock':{'value':3,'status':'obtained'}}}).status_code==200
        def newer(task_id,url):
            return ParsedProduct('123',URL,'新商品标题',skus=[{'source_sku_id':'new','properties':{'颜色':'新'}}],
                images=[{'url':'https://img.alicdn.com/a.png','role':'main'}])
        collector.collect_product=newer
        refreshed=submit(c,tmp_path/'目标','refresh');app.state.worker.run_once()
        doc=c.get(f'/api/collection/products/{pid}/preparation').json()['document']
        assert doc['collected']['fields']['stock']['value']==0
        assert doc['collected']['fields']['stock']['needs_confirmation'] is True
        assert f'skus.{key}.stock' in doc['manual_overrides'] and doc['issues']
        assert c.get(f'/api/collection/jobs/{refreshed}').json()['result']['package_status']=='SAVED'
        collector.collect_product=lambda task_id,url:ParsedProduct('123',URL,'商品',skus=[{'source_sku_id':'old','properties':{'颜色':'旧'}}],images=[{'url':'https://img.alicdn.com/a.png','role':'main'}])
        returned=submit(c,tmp_path/'目标','refresh');app.state.worker.run_once()
        doc=c.get(f'/api/collection/products/{pid}/preparation').json()['document']
        assert doc['collected']['skus'][0]['stock']['value']==0
        assert doc['effective']['skus'][0]['stock']['value']==3
