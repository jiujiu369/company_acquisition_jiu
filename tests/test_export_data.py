import io
import json
import zipfile
import csv
from sqlalchemy import select

from fastapi.testclient import TestClient
from app.main import create_app
from app.products import ProductAsset
from test_collection_tool import attach, URL


def test_standalone_json_only_exposes_remote_media_and_zip_includes_local_files(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        task_id = client.post('/api/collection/jobs', json={'url': URL}).json()['task_id']
        app.state.worker.run_once()
        response = client.get(f'/api/collection/jobs/{task_id}/product.json')
        assert response.status_code == 200
        standalone = response.json()
        assert standalone['export_mode'] == 'metadata_only'
        assert standalone['media'] and standalone['media'][0]['source_url'].startswith('https://')
        assert all('relative_path' not in m and m['file_included'] is False for m in standalone['media'])
        assert all('relative_path' not in b for b in standalone['detail_blocks'])
        assert all('image_relative_path' not in s for s in standalone['skus'])
        response = client.get(f'/api/collection/jobs/{task_id}/export.zip')
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            data = json.loads(archive.read('product.json'))
            assert data['export_mode'] == 'package'
            for m in data['media']:
                if m['file_included']:
                    assert m['relative_path'] in archive.namelist()


def test_zip_contains_each_original_once_instead_of_duplicate_legacy_image_folder(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        _, content = attach(app)
        task_id = client.post('/api/collection/jobs', json={'url': URL}).json()['task_id']
        app.state.worker.run_once()
        response = client.get(f'/api/collection/jobs/{task_id}/export.zip')
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            copies = [name for name in archive.namelist() if archive.read(name) == content]
            assert len(copies) == 1 and copies[0].startswith('assets/')


def test_package_provenance_file_can_be_read_through_download_route(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        task_id = client.post('/api/collection/jobs', json={'url': URL}).json()['task_id']
        app.state.worker.run_once()
        response = client.get(f'/api/collection/jobs/{task_id}/package-file/provenance.json')
        assert response.status_code == 200
        assert 'field_evidence' in response.json()


def test_standalone_csv_exports_latest_values_even_if_cached_media_is_missing(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        attach(app)
        task_id = client.post('/api/collection/jobs', json={'url': URL}).json()['task_id']
        app.state.worker.run_once()
        pid = client.get(f'/api/collection/jobs/{task_id}').json()['result']['product_id']
        client.post(f'/api/collection/products/{pid}/manual', json={
            'overrides': {'fields.title': {'value': '修订后商品名称', 'status': 'obtained'}}})
        with app.state.session_factory() as session:
            asset = session.scalar(select(ProductAsset))
            (tmp_path / asset.relative_path).unlink()
        response = client.get(f'/api/collection/jobs/{task_id}/product.csv')
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        assert rows[0]['宝贝标题'] == '修订后商品名称' and len(rows) == 1
        assert '总库存' not in rows[0]
