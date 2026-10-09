import csv
import io
import json

import pytest

from app.export_data import build_export, sku_csv_row
from app.preparation import build_preparation, apply_overrides, field_value, use_package_profile
from app.taobao.parser import TaobaoParser
from test_preparation_data import product
from test_product_package import sample
from app.product_package import write_package


URL = 'https://item.taobao.com/item.htm?id=123'


def parsed_sku(name, *, price='21.7', stock=42, props=None):
    entry = {'skuId': 'sku1', 'name': name, 'price': price, 'quantity': stock,
             'properties': props or {}}
    html = '<h1>接线端子</h1><ul class="attributes-list"><li>型号：JG</li></ul>'
    html += '<script>window.__INITIAL_STATE__=' + json.dumps({'skus': [entry]}) + ';</script>'
    return TaobaoParser().parse_product(html, URL)


@pytest.mark.parametrize('name,quantity,unit', [
    ('JG10-6（20只）', 20, '只'), ('JG70-6 (10只)', 10, '只'),
    ('JG10-6【20只/包】', 20, '只'), ('20只装 JG10-6', 20, '只'),
    ('JG10-6 每包20只', 20, '只'),
])
def test_pack_quantity_comes_from_sku_label_not_inventory(name, quantity, unit):
    sku = parsed_sku(name).skus[0]
    assert sku['package_quantity'] == quantity
    assert sku['package_unit'] == unit
    assert sku['package_price'] == '21.70'
    assert sku['stock'] == 42  # Original evidence is kept separate from the filling default.


def test_model_numbers_and_conflicting_pack_sizes_are_not_invented():
    assert parsed_sku('JG10-6').skus[0]['package_quantity'] is None
    sku = parsed_sku('JG10-6（20只）', props={'包装': '10只/包'}).skus[0]
    assert sku['package_quantity'] is None


@pytest.mark.parametrize('name', ['JG（10~20只）', 'JG（10～20只）', 'JG（1/2只）', 'JG（10只-20只）'])
def test_count_ranges_and_fractions_stay_unknown(name):
    assert parsed_sku(name).skus[0]['package_quantity'] is None


@pytest.mark.parametrize('key', ['最小起订量', '极数', '库存'])
def test_nonpack_properties_do_not_become_pack_sizes(key):
    assert parsed_sku('JG10-6', props={key: '20只'}).skus[0]['package_quantity'] is None
    assert parsed_sku(None, props={key: '20只'}).skus[0]['package_quantity'] is None


def test_package_price_range_stays_unknown_and_zero_is_kept():
    assert parsed_sku('JG（20只）', price='10-20').skus[0]['package_price'] is None
    assert parsed_sku('JG（20只）', price='0').skus[0]['package_price'] == '0.00'
    assert parsed_sku('JG（20只）', price=0).skus[0]['package_price'] == '0.00'


def test_network_sku_base_and_core_bind_names_to_their_pack_prices():
    envelope = {'item': {'itemId': '123'}, 'skuBase': {'props': [{'pid': 'p', 'name': '规格',
        'values': [{'vid': 'a', 'name': 'JG10-6（20只）'}, {'vid': 'b', 'name': 'JG70-6（10只）'}]}],
        'skus': [{'skuId': 'sku1', 'propPath': 'p:a'}, {'skuId': 'sku2', 'propPath': 'p:b'}]},
        'skuCore': {'sku2info': {'sku1': {'price': {'priceText': '21.70'}, 'quantity': 42},
                               'sku2': {'price': {'priceText': '58.00'}, 'quantity': 0}}}}
    parsed = TaobaoParser().parse_product('<h1>接线端子</h1>', URL, [{'data': envelope}])
    assert [(s['name'], s['package_quantity'], s['package_price']) for s in parsed.skus] == [
        ('JG10-6（20只）', 20, '21.70'), ('JG70-6（10只）', 10, '58.00')]


def test_focused_export_and_csv_have_pack_price_and_default_inventory():
    d = build_preparation(product(), parsed_sku('JG10-6（20只）'))
    data = build_export(d)
    sku = data['skus'][0]
    assert data['fields'] == {'title': '接线端子'}
    assert data['attributes'] == {'型号': 'JG'}
    assert sku['name'] == 'JG10-6（20只）'
    assert sku['package_quantity'] == 20 and sku['package_unit'] == '只'
    assert sku['package_price'] == '21.70' and sku['stock'] == 9999
    assert sku['stock_source'] == 'default_configuration'
    assert 'display_stock' not in sku
    assert not {'barcode', 'sale_status', 'currency'} & data['collection_summary']['unavailable_sku_fields'].keys()
    assert d['completeness']['complete'] is True
    row = sku_csv_row(sku)
    assert row['SKU名称'] == 'JG10-6（20只）'
    assert row['包装数量'] == 20 and row['包装单位'] == '只'
    assert row['每包价格'] == '21.70' and row['库存'] == 9999


def test_national_jg_model_projection_keeps_original_evidence_and_sku_names():
    d = build_preparation(product(), parsed_sku('JG16-12（20只）'))
    attr = d['collected']['attributes'][0]
    assert attr['source_name'] == '型号'
    attr['field']['value'] = '国标A级 JG6-6（20只）'
    projected = apply_overrides(d, {})
    assert projected['effective']['attributes'][0]['field']['value'] == '国标JG'
    assert projected['collected']['attributes'][0]['field']['value'] == '国标A级 JG6-6（20只）'
    assert projected['effective']['skus'][0]['name']['value'] == 'JG16-12（20只）'
    assert build_export(projected)['attributes']['型号'] == '国标JG'
    manual = apply_overrides(projected, {'attributes.型号': field_value('定制JG16-12', source='manual')})
    assert manual['effective']['attributes'][0]['field']['value'] == '定制JG16-12'


def test_manual_pack_correction_and_stock_override_survive():
    d = build_preparation(product(), parsed_sku('JG10-6'))
    sku_id = d['effective']['skus'][0]['local_sku_id']
    d = apply_overrides(d, {f'skus.{sku_id}.package_quantity': field_value(20, source='manual'),
                           f'skus.{sku_id}.package_unit': field_value('只', source='manual'),
                           f'skus.{sku_id}.stock': field_value(0, source='manual')})
    sku = build_export(d)['skus'][0]
    assert sku['package_quantity'] == 20 and sku['stock'] == 0


def test_older_snapshot_is_projected_without_changing_evidence(tmp_path):
    import copy
    p, historical, _ = sample(tmp_path)
    before = copy.deepcopy(historical)
    d = apply_overrides(use_package_profile(historical, [
        {'source_sku_id': 'sku1', 'name': 'JG10-6（20只）'}]), {})
    sku = build_export(d)['skus'][0]
    assert sku['package_quantity'] == 20 and sku['stock'] == 9999
    assert historical == before


def test_source_stock_is_kept_as_evidence_without_replacing_filling_default():
    from app.taobao.seller_read import merge_saved_seller_data
    d = build_preparation(product(), parsed_sku('JG（20只）'))
    d = merge_saved_seller_data(d, {'source_product_id': '123', 'record_kind': 'saved_product',
        'observed_at': '2026-10-09T01:00:00Z', 'source_url': URL,
        'skus': [{'source_sku_id': 'sku1', 'stock': 42}]})
    assert d['collected']['skus'][0]['stock']['value'] == 42
    assert build_export(d)['skus'][0]['stock'] == 9999


def test_historical_capture_uses_its_own_sku_labels_and_prices(tmp_path):
    from app.main import create_app
    from app.product_service import save_collected
    from app.preparation_service import get_document
    from app.products import PreparationCapture
    from app.sku_package import PACKAGE_SKU_FIELDS
    from fastapi.testclient import TestClient
    app = create_app(tmp_path, start_worker=False)
    with TestClient(app), app.state.session_factory.begin() as session:
        original = parsed_sku('JG（20只）', props={'颜色': '蓝'})
        p, _ = save_collected(session, original, 'original-task')
        historical = build_preparation(p, original)
        historical.pop('collection_profile')
        for sku in historical['collected']['skus']:
            for key in PACKAGE_SKU_FIELDS:
                sku.pop(key)
        historical['capture_id'] = 'historical-capture'
        capture = PreparationCapture(id='historical-capture', product_id=p.local_product_id,
            task_id='original-task', kind='source', document=historical)
        session.add(capture)
        session.flush()
        latest = parsed_sku('JG（10只）', price='12', props={'颜色': '蓝'})
        save_collected(session, latest, 'new-task')
        session.flush()
        sku = build_export(get_document(session, p, capture.id))['skus'][0]
        assert sku['name'] == 'JG（20只）'
        assert sku['package_quantity'] == 20 and sku['package_price'] == '21.70'


def test_collection_api_keeps_manual_pack_values_after_refresh(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_collection_tool import attach
    from test_preparation_batches import submit
    app = create_app(tmp_path / 'data', start_worker=False)
    with TestClient(app) as client:
        collector, _ = attach(app)
        original = parsed_sku('JG（20只）')
        original.images = [{'url': 'https://img.alicdn.com/a.png', 'role': 'main'}]
        collector.collect_product = lambda *_: original
        task = submit(client, tmp_path / 'first', mode='refresh')
        app.state.worker.run_once()
        result = client.get(f'/api/collection/jobs/{task}').json()['result']
        assert result['package_status'] == 'SAVED'
        pid = result['product_id']
        detail = client.get(f'/api/collection/products/{pid}').json()
        sku_id = detail['preparation']['effective']['skus'][0]['local_sku_id']
        data = client.get(f'/api/collection/jobs/{task}/product.json').json()
        assert data['skus'][0]['stock'] == 9999
        assert data['skus'][0]['package_quantity'] == 20
        response = client.post(f'/api/collection/products/{pid}/manual', json={'overrides': {
            f'skus.{sku_id}.package_quantity': {'value': 30, 'status': 'obtained'},
            f'skus.{sku_id}.package_price': {'value': '18.80', 'status': 'obtained'},
            f'skus.{sku_id}.stock': {'value': 0, 'status': 'obtained'}}})
        assert response.status_code == 200, response.text
        latest = parsed_sku('JG（10只）', price='12.0', stock=123)
        latest.images = original.images
        collector.collect_product = lambda *_: latest
        new_task = submit(client, tmp_path / 'second', mode='refresh')
        app.state.worker.run_once()
        data = client.get(f'/api/collection/jobs/{new_task}/product.json').json()
        assert data['skus'][0]['name'] == 'JG（10只）'
        assert data['skus'][0]['package_quantity'] == 30
        assert data['skus'][0]['package_price'] == '18.80' and data['skus'][0]['stock'] == 0
        detail = client.get(f'/api/collection/products/{pid}').json()
        assert detail['preparation']['collected']['skus'][0]['buyer_display']['stock'] == 123


@pytest.mark.parametrize('key,value', [('package_quantity', 0), ('package_quantity', -2),
                                     ('package_quantity', 2.5), ('package_price', '-1')])
def test_invalid_pack_corrections_are_rejected(key, value):
    d = build_preparation(product(), parsed_sku('JG（20只）'))
    sku_id = d['effective']['skus'][0]['local_sku_id']
    with pytest.raises(ValueError):
        apply_overrides(d, {f'skus.{sku_id}.{key}': field_value(value, source='manual')})


def test_package_contains_focused_fillable_csv_and_offline_pack_values(tmp_path):
    p, _, _ = sample(tmp_path)
    parsed = parsed_sku('JG10-6（20只）')
    parsed.images = [{'url': p.assets[0].source_url, 'role': 'main'}]
    parsed.skus[0]['image_url'] = p.assets[0].source_url
    d = build_preparation(p, parsed)
    result = write_package(tmp_path, p, d, tmp_path / 'export')
    assert result['status'] == 'SAVED'
    from pathlib import Path
    folder = Path(result['path'])
    rows = list(csv.DictReader(io.StringIO((folder / 'skus.csv').read_text(encoding='utf-8-sig'))))
    assert rows[0]['包装数量'] == '20' and rows[0]['每包价格'] == '21.70'
    assert rows[0]['库存'] == '9999'
    assert '展示库存' not in rows[0] and '条形码' not in rows[0]
    readme = (folder / 'README.html').read_text(encoding='utf-8')
    assert '包装数量' in readme and '每包价格' in readme and '9999' in readme


@pytest.mark.parametrize('include_images', [True, False])
def test_result_dialog_shows_package_values_and_default_stock(tmp_path, include_images):
    from pathlib import Path
    from urllib.parse import urlparse
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright
    from app.main import create_app
    from app.preparation import FIELD_LABELS

    parsed = parsed_sku('JG10-6（20只）')
    doc = build_preparation(product(), parsed)
    doc['include_images'] = include_images
    detail = {'product_id': 'local', 'title': parsed.title, 'source_url': URL,
              'data': {'title': parsed.title, 'model': parsed.model, 'skus': parsed.skus},
              'preparation': doc, 'field_labels': FIELD_LABELS, 'attribute_names': [],
              'package_history': [], 'images': []}
    root = Path(__file__).resolve().parents[1]
    with TestClient(create_app(tmp_path, start_worker=False)) as client, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, channel='chrome')
        page = browser.new_page(viewport={'width': 1200, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def serve(route):
            path = urlparse(route.request.url).path
            if path == '/api/collection/products':
                route.fulfill(json={'items': [{**detail, 'source_product_id': '123',
                    'image_count': 0, 'collected_at': '2026-10-09T00:00:00Z'}], 'total': 1})
            elif path == '/api/collection/products/local':
                route.fulfill(json=detail)
            else:
                response = client.get(path)
                route.fulfill(status=response.status_code, body=response.content,
                              content_type=response.headers.get('content-type', 'application/json'))
        page.route('**/*', serve)
        page.goto('http://fixture.test/products')
        page.get_by_role('button', name='查看结果').click()
        card = page.locator('.sku-card').first
        card.wait_for()
        text = card.inner_text()
        assert '包装数量' in text and '20 只' in text
        assert '每包价格' in text and '21.70' in text
        assert '9999' in text and '42' not in text
        if not include_images:
            assert card.locator('.sku-image').count() == 0
        assert '总库存' not in page.locator('.preparation-panel .detail-grid').inner_text()
        page.get_by_text('人工补充或修正资料', exact=True).click()
        page.locator('#manual-field').select_option(f"skus.{doc['effective']['skus'][0]['local_sku_id']}.package_quantity")
        page.get_by_text('人工补充或修正资料', exact=True).click()
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.sku-card').first.evaluate('e => e.scrollWidth <= e.clientWidth')
        page.screenshot(path=str(root / 'artifacts' / ('sku-package-result.png' if include_images else 'excel-only-result.png')))
        assert not errors
        browser.close()
