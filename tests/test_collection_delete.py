"""Deleting list entries keeps collected data and shared collection work."""
import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.products import CollectionJob, Product, ProductAsset
from app.tasks import Task, TaskStatus
from test_collection_tool import URL, SHOP, attach


def test_delete_completed_task_keeps_originals_history_and_success_reuse(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, content = attach(app)
        tid = client.post('/api/collection/jobs', json={'url':URL}).json()['task_id']
        app.state.worker.run_once()
        with app.state.session_factory() as session:
            product = session.scalar(select(Product))
            pid = product.local_product_id
            asset = session.scalar(select(ProductAsset))
            original, aid = tmp_path/asset.relative_path, asset.id
        assert client.delete(f'/api/collection/jobs/{tid}').status_code == 200
        assert client.get('/api/collection/jobs').json()['total'] == 0
        assert original.read_bytes() == content
        assert client.get(f'/api/collection/products/{pid}/images/{aid}').content == content
        with app.state.session_factory() as session:
            assert session.get(Task, tid).status == TaskStatus.SUCCESS
            assert session.get(CollectionJob, tid) is not None
            assert session.scalar(select(func.count()).select_from(Product)) == 1
            assert session.scalar(select(func.count()).select_from(Task)) == 1
        again = client.post('/api/collection/jobs', json={'url':URL}).json()
        assert again == {'task_id':tid, 'status':'SUCCESS'}
        assert client.get('/api/collection/jobs').json()['total'] == 1
        assert not app.state.worker.run_once()
        assert collector.visits == [URL]


@pytest.mark.parametrize('status', ['PENDING','RUNNING','NEEDS_MANUAL','CANCELED','PARTIAL','FAILED'])
def test_deleted_unfinished_task_stays_removed_after_restart(tmp_path, status):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        tid = client.post('/api/collection/jobs', json={'url':URL}).json()['task_id']
        with app.state.session_factory.begin() as session:
            session.get(Task, tid).status = status
            job = session.get(CollectionJob, tid)
            job.result_data = {**job.result_data, 'manual_browser_owner':True}
        assert client.delete(f'/api/collection/jobs/{tid}').status_code == 200
        assert client.get('/api/collection/jobs').json()['items'] == []
        assert not app.state.worker.run_once()
        assert client.post(f'/api/collection/jobs/{tid}/resume').status_code == 409
        with app.state.session_factory() as session:
            assert not session.get(CollectionJob, tid).result_data['manual_browser_owner']
    restarted = create_app(tmp_path, start_worker=False)
    with TestClient(restarted) as client:
        attach(restarted)
        assert client.get('/api/collection/jobs').json()['total'] == 0
        assert not restarted.state.worker.run_once()
        again = client.post('/api/collection/jobs', json={'url':URL}).json()
        assert again['status'] == 'PENDING'
        assert client.get('/api/collection/jobs').json()['total'] == 1


def test_deletion_during_capture_stops_persisting_and_worker_cannot_revive_it(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, _ = attach(app)
        collect = collector.collect_product
        def delete_during_capture(tid, url):
            assert client.delete(f'/api/collection/jobs/{tid}').status_code == 200
            return collect(tid, url)
        collector.collect_product = delete_during_capture
        tid = client.post('/api/collection/jobs', json={'url':URL}).json()['task_id']
        assert app.state.worker.run_once()
        assert not app.state.worker.run_once()
        assert client.get('/api/collection/products').json()['total'] == 0
        with app.state.session_factory() as session:
            assert session.get(Task, tid).status == TaskStatus.CANCELED
        assert client.get('/api/collection/jobs').json()['total'] == 0


def test_delete_shop_keeps_shared_child_until_last_consumer_is_deleted(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        ids = client.post('/api/collection/jobs/batch', json={'urls':[SHOP, SHOP.replace('shop123', 'shop456')]}).json()['task_ids']
        app.state.worker.run_once()
        app.state.worker.run_once()
        with app.state.session_factory.begin() as session:
            child = session.scalar(select(CollectionJob).where(CollectionJob.kind == 'product'))
            cid = child.task_id
            child.result_data = {**child.result_data, 'manual_browser_owner':True}
        assert client.delete(f'/api/collection/jobs/{ids[0]}').status_code == 200
        with app.state.session_factory() as session:
            assert session.get(Task, cid).status == 'PENDING'
        assert client.get('/api/collection/jobs').json()['total'] == 1
        assert client.delete(f'/api/collection/jobs/{ids[1]}').status_code == 200
        with app.state.session_factory() as session:
            assert session.get(Task, cid).status == 'CANCELED'
            assert not session.get(CollectionJob, cid).result_data['manual_browser_owner']
        assert not app.state.worker.run_once()
        again = client.post('/api/collection/jobs', json={'url':SHOP}).json()['task_id']
        for _ in range(8):
            if app.state.worker.run_once():
                continue
            with app.state.session_factory.begin() as session:
                for task in session.scalars(select(Task).where(Task.status == 'PENDING')):
                    task.available_at = None
        assert client.get(f'/api/collection/jobs/{again}').json()['task']['status'] == 'SUCCESS'


def test_delete_shop_does_not_stop_individually_requested_product(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        shop_id = client.post('/api/collection/jobs', json={'url':SHOP}).json()['task_id']
        app.state.worker.run_once()
        tid = client.post('/api/collection/jobs', json={'url':URL}).json()['task_id']
        assert client.delete(f'/api/collection/jobs/{shop_id}').status_code == 200
        app.state.worker.run_once()
        assert client.get(f'/api/collection/jobs/{tid}').json()['task']['status'] == 'SUCCESS'
        assert client.get('/api/collection/jobs').json()['total'] == 1


def test_batch_delete_is_atomic_idempotent_and_leaves_other_page_entries(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        ids = client.post('/api/collection/jobs/batch', json={'urls':[URL, URL.replace('123','456'), URL.replace('123','789')]}).json()['task_ids']
        assert client.post('/api/collection/jobs/delete', json={'task_ids':[ids[0], 'missing']}).status_code == 404
        assert client.get('/api/collection/jobs').json()['total'] == 3
        response = client.post('/api/collection/jobs/delete', json={'task_ids':[ids[0], ids[1], ids[0]]})
        assert response.status_code == 200
        assert response.json()['deleted_count'] == 2
        assert [row['task']['task_id'] for row in client.get('/api/collection/jobs').json()['items']] == [ids[2]]
        assert client.delete(f'/api/collection/jobs/{ids[0]}').status_code == 200
        with app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Task)) == 3


def test_deleted_partial_task_reuses_capture_and_download_checkpoint(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, content = attach(app)
        collect = collector.collect_product
        def two_images(tid, url):
            parsed = collect(tid, url)
            parsed.images.append({'url':'https://img.alicdn.com/second.png', 'role':'detail'})
            return parsed
        collector.collect_product = two_images
        store = app.state.collector_runner.asset_store
        download = store.download_original
        failed = True
        def partial(pid, url, role, **kwargs):
            if failed and url.endswith('second.png'):
                raise OSError('temporary image failure')
            return download(pid, url, role, **kwargs)
        store.download_original = partial
        tid = client.post('/api/collection/jobs', json={'url':URL}).json()['task_id']
        app.state.worker.run_once()
        with app.state.session_factory() as session:
            asset = session.scalar(select(ProductAsset))
            path = tmp_path/asset.relative_path
        assert client.delete(f'/api/collection/jobs/{tid}').status_code == 200
        failed = False
        assert client.post('/api/collection/jobs', json={'url':URL}).json()['status'] == 'PENDING'
        app.state.worker.run_once()
        assert client.get(f'/api/collection/jobs/{tid}').json()['task']['status'] == 'SUCCESS'
        assert collector.visits == [URL]
        assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(content).digest()


def shared_partial(client, app):
    attach(app)
    store = app.state.collector_runner.asset_store
    download = store.download_original
    def unavailable(*args, **kwargs):
        raise OSError('temporary image failure')
    store.download_original = unavailable
    ids = client.post('/api/collection/jobs/batch', json={'urls':[SHOP, SHOP.replace('shop123','shop456')]}).json()['task_ids']
    for _ in range(10):
        if app.state.worker.run_once():
            continue
        with app.state.session_factory.begin() as session:
            for task in session.scalars(select(Task).where(Task.status == 'PENDING')):
                task.available_at = None
    assert [client.get(f'/api/collection/jobs/{tid}').json()['task']['status'] for tid in ids] == ['PARTIAL','PARTIAL']
    with app.state.session_factory() as session:
        cid = session.scalar(select(CollectionJob).where(CollectionJob.kind == 'product')).task_id
    store.download_original = download
    return ids, cid


def test_shared_child_retry_does_not_requeue_deleted_partial_shop(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        ids, cid = shared_partial(client, app)
        client.delete(f'/api/collection/jobs/{ids[0]}')
        assert client.post(f'/api/collection/jobs/{cid}/retry').status_code == 200
        with app.state.session_factory() as session:
            assert session.get(Task, ids[0]).status == 'PARTIAL'
            assert session.get(Task, ids[1]).status == 'PENDING'


def test_pause_last_retained_shop_stops_shared_child_after_other_shop_deleted(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        ids, cid = shared_partial(client, app)
        client.delete(f'/api/collection/jobs/{ids[0]}')
        client.post(f'/api/collection/jobs/{ids[1]}/retry')
        assert client.post(f'/api/collection/jobs/{ids[1]}/pause').status_code == 200
        with app.state.session_factory() as session:
            assert session.get(Task, cid).status == 'CANCELED'
        assert not app.state.worker.run_once()


def test_runner_ignores_deleted_parent_when_remaining_shop_is_paused(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        ids, cid = shared_partial(client, app)
        client.delete(f'/api/collection/jobs/{ids[0]}')
        client.post(f'/api/collection/jobs/{ids[1]}/retry')
        # Model the parent pause becoming visible before its child state update.
        with app.state.session_factory.begin() as session:
            session.get(Task, ids[1]).status = 'CANCELED'
        assert app.state.worker.run_once()
        with app.state.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(ProductAsset)) == 0
            assert session.get(Task, cid).status == 'PENDING'
