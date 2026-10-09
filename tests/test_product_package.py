import io
import json
import hashlib
import shutil
import csv
import copy
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from PIL import Image
from app.preparation import build_preparation, apply_overrides, field_value
from app.product_package import write_package, verify_package
from app.taobao.parser import ParsedProduct
from app.sku_package import PACKAGE_SKU_FIELDS


def sample(tmp_path):
    original = tmp_path / 'cache' / 'original.png'
    original.parent.mkdir()
    buf = io.BytesIO(); Image.new('RGB', (8, 8), 'blue').save(buf, 'PNG')
    original.write_bytes(buf.getvalue())
    asset = SimpleNamespace(id='asset', source_url='https://img.alicdn.com/a.png', relative_path='cache/original.png',
        sha256=hashlib.sha256(buf.getvalue()).hexdigest(), byte_size=len(buf.getvalue()), width=8, height=8,
        original_asset_id=None, role='main', kind='image')
    p = SimpleNamespace(local_product_id='local', source_product_id='123', source_platform='taobao',
        source_url='https://item.taobao.com/item.htm?id=123', collected_at=datetime(2026, 10, 5),
        title='中文 / 商品:*', category=None, price=None, attributes={}, current_data={}, raw_data={}, assets=[asset])
    parsed = ParsedProduct('123', p.source_url, p.title, images=[{'url': asset.source_url, 'role': 'main'},
        {'url': asset.source_url, 'role': 'detail'}], detail_blocks=[{'type': 'text', 'text': '<script>说明</script>'},
        {'type': 'image', 'url': asset.source_url}, {'type': 'text', 'text': '后'}, {'type': 'image', 'url': asset.source_url}],
        skus=[{'source_sku_id': 'sku1', 'properties': {'颜色': '蓝'}, 'image_url': asset.source_url}])
    # Preserve coverage for exports of pre-package-profile snapshots.
    # The current focused workflow has its own fixtures in test_sku_package_collection.py.
    doc = build_preparation(p, parsed)
    doc.pop('collection_profile')
    for sku in doc['collected']['skus']:
        sku['stock'] = field_value(reason='历史记录未取得库存')
        for key in PACKAGE_SKU_FIELDS:
            sku.pop(key, None)
    return p, apply_overrides(doc, {}), buf.getvalue()


def test_complete_package_moves_and_keeps_history(tmp_path):
    p, d, content = sample(tmp_path)
    result = write_package(tmp_path, p, d, tmp_path / '中文 保存')
    assert result['status'] == 'SAVED'
    path = __import__('pathlib').Path(result['path'])
    for f in ('product.json', 'product.csv', 'skus.csv', 'README.html', 'detail.html', 'media_manifest.json', 'sources.json', 'issues.json', 'package.complete.json'):
        assert (path / f).is_file()
    assert (path / 'assets/asset.png').read_bytes() == content
    assert (path / 'detail.html').read_text(encoding='utf-8').count('src="assets/asset.png"') == 2
    assert '<script>' not in (path / 'detail.html').read_text(encoding='utf-8')
    moved = tmp_path / '移动 后'; shutil.copytree(path, moved)
    assert verify_package(moved)['status'] == 'SAVED'
    second = write_package(tmp_path, p, d, tmp_path / '中文 保存')
    assert second['path'] != result['path'] and path.exists()
    manifest = json.loads((moved / 'product.json').read_text(encoding='utf-8'))
    assert manifest['skus'][0]['image_relative_path'] == 'assets/asset.png'


def test_corrupt_original_not_reported_saved(tmp_path):
    p, d, _ = sample(tmp_path)
    (tmp_path / 'cache/original.png').write_bytes(b'bad')
    r = write_package(tmp_path, p, d, tmp_path / '目标')
    assert r['status'] == 'FAILED'
    assert not __import__('pathlib').Path(r['path']).joinpath('package.complete.json').exists()


def test_video_original_bytes_are_preserved(tmp_path):
    from app.assets import AssetStore
    content=b'\x00\x00\x00\x18ftypmp42'+b'\x00'*32
    asset=AssetStore(tmp_path).save_original_bytes('local','https://cloud.video.taobao.com/a.mp4','product_video','video/mp4',content)
    assert asset.kind=='video' and asset.width is None
    assert (tmp_path/asset.relative_path).read_bytes()==content


def test_excluded_fields_never_enter_exports_and_long_names_are_safe(tmp_path):
    p,d,_=sample(tmp_path)
    p.title='CON:*? / '+('很长的商品名称'*50)
    p.attributes={'服务':{'shipping_address':'上海','freight_template':{'free_shipping':'是'},'质保':'一年'}}
    d=build_preparation(p)
    r=write_package(tmp_path,p,d,tmp_path/'目标')
    assert r['status']=='SAVED'
    from pathlib import Path
    path=Path(r['path'])
    assert len(path.parent.name)<80 and not any(x in path.parent.name for x in '<>:"/\\|?*')
    for filename in ('product.json','product.csv','skus.csv'):
        text=(path/filename).read_text(encoding='utf-8-sig')
        assert all(name not in text for name in ('shipping_address','freight_template','free_shipping'))


def test_export_is_compact_and_keeps_display_values_in_readable_columns(tmp_path):
    from app.preparation import field_value
    p, doc, _ = sample(tmp_path)
    doc['effective']['attributes'] = [{'source_name': '品牌', 'raw_value': '施科特',
        'field': field_value('施科特', source='public_product_page')}]
    sku = doc['effective']['skus'][0]
    sku['buyer_display'] = {'price': '16.00', 'stock': 200, 'price_raw': {'priceTitle': '优惠前'}}
    before = copy.deepcopy(doc)
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    folder = Path(result['path'])
    data = json.loads((folder / 'product.json').read_text(encoding='utf-8'))
    assert data['fields'] == {'title': p.title}
    assert data['attributes'] == {'品牌': '施科特'}
    assert not {'collected', 'effective', 'images', 'manual_overrides'} & data.keys()
    assert data['skus'][0]['display_price'] == '16.00'
    assert data['skus'][0]['display_price_type'] == '优惠前'
    assert data['skus'][0]['display_stock'] == 200
    assert 'price' not in data['skus'][0] and 'stock' not in data['skus'][0]
    assert 'barcode' not in data['skus'][0]
    with (folder / 'skus.csv').open(encoding='utf-8-sig', newline='') as stream:
        row = list(csv.DictReader(stream))[0]
    assert row['展示价格'] == '16.00' and row['展示库存'] == '200'
    assert row['展示价格类型'] == '优惠前'
    assert row['记录价格'] == '' and row['记录库存'] == ''
    assert row['规格'] == '颜色: 蓝' and row['图片文件'] == 'assets/asset.png'
    readme = (folder / 'README.html').read_text(encoding='utf-8')
    assert '16.00' in readme and '优惠前' in readme and '200' in readme
    assert doc == before  # Export must not alter the immutable capture.


def test_export_preserves_manual_zero_and_distinguishes_display_price(tmp_path):
    from app.preparation import apply_overrides, field_value
    p, doc, _ = sample(tmp_path)
    sku_id = doc['effective']['skus'][0]['local_sku_id']
    doc['collected']['skus'][0]['buyer_display'] = {'price': '16.00', 'stock': 200, 'price_raw': None}
    doc = apply_overrides(doc, {
        f'skus.{sku_id}.price': field_value('12.50', source='manual'),
        f'skus.{sku_id}.stock': field_value(0, source='manual'),
        'fields.title': field_value('人工标题', source='manual')})
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    folder = Path(result['path'])
    data = json.loads((folder / 'product.json').read_text(encoding='utf-8'))
    assert data['fields']['title'] == '人工标题'
    assert data['skus'][0]['price'] == '12.50' and data['skus'][0]['stock'] == 0
    assert data['skus'][0]['display_price'] == '16.00'
    provenance = json.loads((folder / 'provenance.json').read_text(encoding='utf-8'))
    assert provenance['manual_overrides']['fields.title']['value'] == '人工标题'
    with (folder / 'product.csv').open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]['宝贝标题'] == '人工标题'


def test_package_rewrites_stale_asset_paths_to_actual_copied_files(tmp_path):
    p, doc, _ = sample(tmp_path)
    for media in doc['media']:
        media['relative_path'] = 'assets/stale.jpg'
    for block in doc['detail_blocks']:
        if block['type'] == 'image':
            block['relative_path'] = 'assets/stale.jpg'
    doc['effective']['skus'][0]['image_relative_path'] = 'assets/stale.jpg'
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    assert result['status'] == 'SAVED'
    folder = Path(result['path'])
    data = json.loads((folder / 'product.json').read_text(encoding='utf-8'))
    refs = [m['relative_path'] for m in data['media']]
    refs += [b['relative_path'] for b in data['detail_blocks'] if b['type'] == 'image']
    refs += [s['image_relative_path'] for s in data['skus']]
    assert set(refs) == {'assets/asset.png'}
    assert all((folder / ref).is_file() for ref in refs)


def test_verifier_rejects_untracked_or_missing_json_asset_reference(tmp_path):
    p, doc, _ = sample(tmp_path)
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    folder = Path(result['path'])
    data = json.loads((folder / 'product.json').read_text(encoding='utf-8'))
    data['media'][0]['relative_path'] = 'assets/missing.png'
    (folder / 'product.json').write_text(json.dumps(data), encoding='utf-8')
    complete = json.loads((folder / 'package.complete.json').read_text(encoding='utf-8'))
    complete['files']['product.json'] = hashlib.sha256((folder / 'product.json').read_bytes()).hexdigest()
    (folder / 'package.complete.json').write_text(json.dumps(complete), encoding='utf-8')
    assert verify_package(folder)['status'] == 'FAILED'


def test_failed_media_is_explicit_and_has_no_fake_local_reference(tmp_path):
    p, doc, _ = sample(tmp_path)
    doc['media'].append({'role': 'sku', 'order': 2, 'asset_id': None,
        'source_url': 'https://img.alicdn.com/missing.png', 'relative_path': 'assets/fake.png',
        'status': 'failed', 'reason': 'HTTP 403', 'is_first': False})
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    folder = Path(result['path'])
    data = json.loads((folder / 'product.json').read_text(encoding='utf-8'))
    assert result['status'] == 'INCOMPLETE'
    assert data['media'][-1]['file_included'] is False
    assert 'relative_path' not in data['media'][-1]
    assert not (folder / 'package.complete.json').exists()


def test_pending_values_keep_confirmation_flags_in_practical_exports(tmp_path):
    from app.preparation import apply_overrides, field_value
    p, doc, _ = sample(tmp_path)
    doc = apply_overrides(doc, {
        'fields.price': field_value('13.00', source='manual', needs_confirmation=True),
        'attributes.安装方式': field_value('压接', source='manual', needs_confirmation=True)})
    doc['media'][0]['needs_confirmation'] = True
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    folder = Path(result['path'])
    data = json.loads((folder / 'product.json').read_text(encoding='utf-8'))
    assert data['collection_summary']['pending_attributes'] == ['安装方式']
    assert data['media'][0]['needs_confirmation'] is True
    with (folder / 'product.csv').open(encoding='utf-8-sig', newline='') as stream:
        row = list(csv.DictReader(stream))[0]
    assert '一口价' in row['待确认字段'] and '安装方式' in row['待确认字段']
    assert row['一口价'] == '13.00'


def test_verifier_checks_sku_path_even_without_source_url(tmp_path):
    p, doc, _ = sample(tmp_path)
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    folder = Path(result['path'])
    data = json.loads((folder / 'product.json').read_text(encoding='utf-8'))
    data['skus'][0].pop('image_source_url')
    data['skus'][0]['image_relative_path'] = 'assets/missing.png'
    (folder / 'product.json').write_text(json.dumps(data), encoding='utf-8')
    complete = json.loads((folder / 'package.complete.json').read_text(encoding='utf-8'))
    complete['files']['product.json'] = hashlib.sha256((folder / 'product.json').read_bytes()).hexdigest()
    (folder / 'package.complete.json').write_text(json.dumps(complete), encoding='utf-8')
    assert verify_package(folder)['status'] == 'FAILED'


def test_explicit_nonvalue_states_survive_standalone_json_and_csv(tmp_path):
    from app.preparation import apply_overrides, field_value
    from app.export_data import build_export
    p, doc, _ = sample(tmp_path)
    sku_id = doc['effective']['skus'][0]['local_sku_id']
    doc = apply_overrides(doc, {
        'fields.barcode': field_value(status='not_applicable', source='manual', reason='此商品无条码要求'),
        f'skus.{sku_id}.merchant_code': field_value(status='not_set', source='manual', reason='原商品未设置')})
    standalone = build_export(doc)
    assert standalone['field_states']['fields.barcode'] == {'status': 'not_applicable', 'reason': '此商品无条码要求'}
    assert 'barcode' not in standalone['collection_summary']['missing_fields']
    assert standalone['skus'][0]['field_states']['merchant_code']['status'] == 'not_set'
    assert 'merchant_code' not in standalone['collection_summary']['unavailable_sku_fields']
    result = write_package(tmp_path, p, doc, tmp_path / 'export')
    folder = Path(result['path'])
    with (folder / 'product.csv').open(encoding='utf-8-sig', newline='') as stream:
        row = list(csv.DictReader(stream))[0]
    assert '不适用' in row['状态说明'] and '此商品无条码要求' in row['状态说明']
    with (folder / 'skus.csv').open(encoding='utf-8-sig', newline='') as stream:
        row = list(csv.DictReader(stream))[0]
    assert '明确未设置' in row['状态说明'] and '原商品未设置' in row['状态说明']
