import io
import zipfile
import re
from pathlib import Path
from urllib.parse import unquote
import pytest

from fastapi.testclient import TestClient

from app.main import create_app
from app.taobao.parser import TaobaoParser
from test_collection_tool import attach, URL, SHOP


@pytest.mark.parametrize(('title', 'filename'), [
    ('铜端子 JG16/12:20只', '铜端子 JG16_12_20只.xlsx'),
    ('铜端子 #1', '铜端子 #1.xlsx'),
])
def test_product_name_download_and_package_use_the_effective_title(tmp_path, title, filename):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, _ = attach(app)
        collector.collect_product = lambda task, url: TaobaoParser().parse_product(
            '<h1>' + title + '</h1>', url)
        tid = client.post('/api/collection/jobs', json={'url': URL, 'collect_images': False}).json()['task_id']
        app.state.worker.run_once()
        view = client.get(f'/api/collection/jobs/{tid}').json()
        folder = Path(view['result']['package_path'])
        assert (folder / filename).is_file()
        href = re.search(r'<a href="([^"]+)">下载商品资料 Excel',
                         (folder / 'README.html').read_text(encoding='utf-8')).group(1)
        assert '#' not in href and unquote(href) == filename
        response = client.get(f'/api/collection/jobs/{tid}/export.xlsx')
        assert filename in unquote(response.headers['content-disposition'])


def test_shop_excel_download_keeps_each_product_in_its_own_named_workbook(tmp_path):
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app) as client:
        collector, _ = attach(app)
        collector.discover_shop = lambda task, url: [URL, URL.replace('123', '456')]
        collector.collect_product = lambda task, url: TaobaoParser().parse_product(
            '<h1>' + ('铜端子' if '123' in url else '接线排') + '</h1>', url)
        tid = client.post('/api/collection/jobs', json={'url': SHOP, 'collect_images': False}).json()['task_id']
        for _ in range(8):
            app.state.worker.run_once()
        response = client.get(f'/api/collection/jobs/{tid}/export.xlsx')
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('application/zip')
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            assert bundle.namelist() == ['铜端子.xlsx', '接线排.xlsx']
            for name in bundle.namelist():
                with zipfile.ZipFile(io.BytesIO(bundle.read(name))) as book:
                    assert book.read('xl/worksheets/sheet1.xml').count(b'https://item.taobao.com/item.htm?id=') == 1
