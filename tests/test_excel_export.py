"""Excel exports must preserve source meaning at the final file boundary."""
import io
import base64
import zipfile
from xml.etree import ElementTree as ET

import pytest


NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def known(value, source='public_product_page'):
    return {'value': value, 'status': 'obtained', 'source_refs': [source]}


def document():
    return {
        'source_product_id': '001234',
        'sources': [{'id': 'public_product_page', 'url': 'https://item.taobao.com/item.htm?id=001234'}],
        'buyer_display': {'price': '88.50'},
        'effective': {
            'fields': {'title': known('铜接线端子'), 'price': known('90.00', 'manual'),
                       'stock': {'value': None, 'status': 'not_obtained'}},
            'attributes': [
                {'source_name': '安装方式', 'field': known('螺丝固定')},
                {'source_name': '导体材质', 'field': known('紫铜')},
                {'source_name': '货号', 'field': known('JG10-6')},
                {'source_name': '导线截面积', 'field': known('10 mm²')},
                {'source_name': '适用线缆类型', 'field': known('铜芯线')},
            ],
            'skus': [
                {'source_sku_id': '0001', 'name': known('JG10-6 / 100只装'),
                 'package_price': known('25.80'), 'package_quantity': known(100),
                 'package_unit': known('只'), 'stock': known(9999, 'default_configuration')},
                {'source_sku_id': '0002', 'name': known('JG16-8 / 50只装'),
                 'package_price': known(0), 'package_quantity': known(50),
                 'package_unit': known('只'), 'stock': known(9999, 'default_configuration')},
            ],
        },
    }


def exported(documents, main_images=None):
    from app.excel_export import excel_bytes
    content = excel_bytes(documents, main_images=main_images)
    assert isinstance(content, bytes)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings = [''.join(si.itertext()) for si in ET.fromstring(archive.read('xl/sharedStrings.xml'))]
        result = []
        cell_types = []
        for row in sheet.findall('m:sheetData/m:row', NS):
            values, types = {}, {}
            for cell in row.findall('m:c', NS):
                col = ''.join(x for x in cell.attrib['r'] if x.isalpha())
                types[col] = cell.get('t', 'n')
                if cell.get('t') == 'inlineStr':
                    values[col] = ''.join(cell.find('m:is', NS).itertext())
                elif cell.find('m:v', NS) is not None:
                    raw = cell.find('m:v', NS).text
                    values[col] = (strings[int(raw)] if cell.get('t') == 's'
                                   else raw if cell.get('t') == 'str' else float(raw))
                else:
                    values[col] = None
            result.append(values)
            cell_types.append(types)
        headers = result[0]
        rows = [{headers[col]: row.get(col) for col in headers} for row in result[1:]]
        tables = [ET.fromstring(archive.read(n)) for n in archive.namelist()
                  if n.startswith('xl/tables/') and n.endswith('.xml')]
        return rows, sheet, tables, result, cell_types


def test_all_skus_retain_full_package_prices_attributes_and_fixed_settings():
    rows, sheet, tables, _, types = exported([document()])
    assert len(rows) == 2
    assert [r['销售规格'] for r in rows] == ['JG10-6 / 100只装', 'JG16-8 / 50只装']
    assert [r['价格'] for r in rows] == [25.8, 0]
    assert [r['数量'] for r in rows] == [100, 50]
    for row in rows:
        assert row['商品名称'] == '铜接线端子'
        assert row['安装方式'] == '螺丝固定'
        assert row['导线材质'] == '紫铜'
        assert row['型号'] == 'JG10-6'
        assert row['导线截面积'] == '10 mm²'
        assert row['适用线缆类型'] == '铜芯线'
        assert row['一口价'] == 90
        assert row['总库存'] is None
        assert row['上架时间'] == '放入仓库'
        assert row['发货时间'] == '48小时'
        assert row['发货地'] == '大陆及港澳台  单一发货地  浙江温州'
        assert row['提取方式'] == '使用物流配送'
        assert row['运费'] == '包邮'
    assert list(rows[0]) == ['商品名称','商品主图','安装方式','导线材质','型号','销售规格','SKU编号',
                             '价格','数量','包装单位','导线截面积','适用线缆类型','一口价','总库存',
                             '上架时间','发货时间','发货地','提取方式','运费','来源链接']
    assert [r['来源链接'] for r in rows] == ['https://item.taobao.com/item.htm?id=001234', None]
    assert [r['SKU编号'] for r in rows] == ['JG10-6', 'JG16-8']
    assert types[1]['H'] == types[1]['I'] == 'n'
    assert sheet.find('m:sheetViews/m:sheetView/m:pane', NS).get('ySplit') == '1'
    assert tables[0].get('ref') == 'A1:T3'
    assert tables[0].find('m:autoFilter', NS).get('ref') == 'A1:T3'


def test_unknown_values_are_blank_and_known_zero_stock_is_preserved():
    d = document()
    d['effective']['fields']['stock'] = known(0, 'manual')
    d['effective']['attributes'][0]['field'] = {'status': 'failed', 'value': 'stale'}
    d['effective']['skus'][0]['package_price'] = {'status': 'not_obtained', 'value': None}
    d['effective']['skus'][0]['package_quantity'] = {'status': 'not_obtained', 'value': None}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['总库存'] == 0
    assert rows[0]['安装方式'] is None
    assert rows[0]['价格'] is None
    assert rows[0]['数量'] is None


@pytest.mark.parametrize('value', ['国标A级 JG6-6（20只）', '国标JG16-12/20只', 'JG16-12'])
def test_national_jg_product_model_is_series_without_sku_suffix(value):
    d = document()
    d['effective']['fields']['title'] = known('国标JG铜接线端子')
    d['effective']['attributes'][2] = {'source_name': '型号', 'field': known(value)}
    rows, _, _, _, _ = exported([d])
    assert all(r['型号'] == '国标JG' for r in rows)
    assert rows[0]['SKU编号'] == 'JG10-6'
    assert rows[1]['SKU编号'] == 'JG16-8'
    assert rows[0]['销售规格'] == 'JG10-6 / 100只装'


def test_other_models_and_manual_product_models_are_not_replaced_with_national_jg():
    d = document()
    d['effective']['fields']['title'] = known('国标端子')
    d['effective']['attributes'][2] = {'source_name': '型号', 'field': known('DT10')}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['型号'] == 'DT10'
    d['effective']['attributes'][2]['field'] = known('定制JG16-12', 'manual')
    rows, _, _, _, _ = exported([d])
    assert rows[0]['型号'] == '定制JG16-12'


@pytest.mark.parametrize('product_price,expected', [('88.50', 88.5), ('0', 0), ('12-88', None), (None, None)])
def test_product_price_uses_only_explicit_single_product_value(product_price, expected):
    d = document()
    d['effective']['fields']['price'] = {'status': 'not_obtained', 'value': None}
    d['buyer_display']['price'] = product_price
    rows, _, _, _, _ = exported([d])
    assert rows[0]['一口价'] == expected


def test_default_product_inventory_is_not_actual_total_stock():
    d = document()
    d['effective']['fields']['stock'] = known(9999, 'default_configuration')
    rows, _, _, _, _ = exported([d])
    assert rows[0]['总库存'] is None


def test_product_display_minimum_with_range_evidence_is_not_a_single_price():
    d = document()
    d['effective']['fields']['price'] = {'status': 'not_obtained', 'value': None}
    d['buyer_display'] = {'price': '12.00', 'price_raw': '¥12.00-88.50'}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['一口价'] is None


def test_sku_specific_wire_attributes_override_product_fallback_without_model_inference():
    d = document()
    d['effective']['skus'][0]['combination'] = {'导线截面积': '16 mm²', '适用线缆类型': '多股铜线'}
    d['effective']['skus'][1]['combination'] = {'型号': 'JG16-8', '颜色': '蓝'}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['导线截面积'] == '16 mm²'
    assert rows[0]['适用线缆类型'] == '多股铜线'
    assert rows[1]['导线截面积'] == '10 mm²'
    assert rows[1]['适用线缆类型'] == '铜芯线'
    d['effective']['attributes'] = []
    rows, _, _, _, _ = exported([d])
    assert rows[1]['导线截面积'] is None
    assert rows[1]['适用线缆类型'] is None


@pytest.mark.parametrize('alias', ['导线截面', '标称截面积'])
def test_wire_attribute_semantic_aliases_and_unknown_sku_value_use_product_fallback(alias):
    d = document()
    d['effective']['attributes'][3]['source_name'] = alias
    d['effective']['attributes'][4]['source_name'] = '适用电缆类型'
    d['effective']['skus'][0]['combination'] = {'导线截面积': None, '适用线缆类型': ''}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['导线截面积'] == '10 mm²'
    assert rows[0]['适用线缆类型'] == '铜芯线'


@pytest.mark.parametrize('status', ['not_set', 'failed', 'not_applicable'])
def test_explicit_nonvalue_product_price_states_do_not_fall_back_to_public_price(status):
    d = document()
    d['effective']['fields']['price'] = {'status': status, 'value': None, 'source_refs': ['manual']}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['一口价'] is None


def test_manual_unknown_product_price_override_remains_blank():
    d = document()
    d['effective']['fields']['price'] = {'status': 'not_obtained', 'value': None}
    d['manual_overrides'] = {'fields.price': {'status': 'not_obtained', 'value': None}}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['一口价'] is None


def test_large_export_preserves_every_sku_and_resizes_table_to_last_row():
    d = document()
    d['effective']['skus'] = [dict(d['effective']['skus'][0], source_sku_id=str(i),
                                 name=known(f'JG{i+1}-6 / 100只装')) for i in range(1250)]
    rows, sheet, tables, _, _ = exported([d])
    assert len(rows) == 1250
    assert [r['SKU编号'] for r in rows] == [f'JG{i+1}-6' for i in range(1250)]
    assert sum(r['来源链接'] is not None for r in rows) == 1
    assert tables[0].get('ref') == 'A1:T1251'
    assert sheet.find('m:dimension', NS).get('ref') == 'A1:T1251'


def test_products_without_skus_and_empty_export_keep_structure():
    d = document()
    d['effective']['skus'] = []
    rows, _, _, _, _ = exported([d])
    assert len(rows) == 1
    for row in rows:
        assert row['销售规格'] is row['价格'] is row['数量'] is row['包装单位'] is None
    rows, sheet, tables, _, _ = exported([])
    assert rows == []
    assert sheet.find('m:dimension', NS).get('ref') == 'A1:T1'
    assert tables[0].get('ref') == 'A1:T1'


def test_untrusted_strings_are_literal_text_never_formulas():
    d = document()
    attack = '=HYPERLINK("https://example.invalid", "click")'
    d['effective']['fields']['title'] = known(attack)
    d['effective']['skus'][0]['name'] = known('+CMD|\'/C calc\'!A0')
    d['effective']['skus'][0]['package_price'] = known('=1+2')
    rows, sheet, _, _, types = exported([d])
    assert rows[0]['商品名称'] == attack
    assert rows[0]['销售规格'] == '+CMD|\'/C calc\'!A0'
    assert rows[0]['价格'] == '=1+2'
    assert types[1]['A'] == types[1]['H'] == 'inlineStr'
    assert sheet.findall('.//m:f', NS) == []


def test_single_product_workbook_rejects_multiple_documents():
    from app.excel_export import excel_bytes
    with pytest.raises(ValueError, match='单个商品|one product'):
        excel_bytes([document(), document()])


@pytest.mark.parametrize('label,expected', [
    ('JG16-12（20只）', 'JG16-12'), ('DT10铜色【1只】', 'DT10'),
    ('JG16-12/20只', 'JG16-12'), ('JG16-12/20只/包', 'JG16-12'),
    ('JG16-12/20-50只/包', 'JG16-12'),
    ('蓝色 / 100只装', None), ('5423188520209', None),
    ('JG16-12 / JG25-12', None),
])
def test_sku_number_extracts_only_an_unambiguous_model_from_full_label(label, expected):
    d = document()
    d['effective']['skus'][0]['name'] = known(label)
    rows, _, _, _, _ = exported([d])
    assert rows[0]['销售规格'] == label
    assert rows[0]['SKU编号'] == expected


@pytest.mark.parametrize('key', ['型号', '货号', '编号', 'SKU编号'])
def test_explicit_sku_model_has_priority_over_label_and_never_uses_source_id(key):
    d = document()
    d['effective']['skus'][0]['combination'] = {key: 'JG16-12'}
    rows, _, _, _, _ = exported([d])
    assert rows[0]['SKU编号'] == 'JG16-12'
    assert rows[0]['销售规格'] == 'JG10-6 / 100只装'


@pytest.mark.parametrize('key', ['型号', '货号', '编号', 'SKU编号'])
def test_explicit_sku_model_removes_pack_label_from_number_only(key):
    d = document()
    d['effective']['skus'][0]['combination'] = {key: 'JG16-12（20只）'}
    d['effective']['skus'][0]['name'] = known('JG16-12（20只）')
    rows, _, _, _, _ = exported([d])
    assert rows[0]['SKU编号'] == 'JG16-12'
    assert rows[0]['销售规格'] == 'JG16-12（20只）'


@pytest.mark.parametrize('label', ['JG16-12/20只', 'JG16-12/20只/包', 'JG16-12/20-50只/包'])
def test_explicit_sku_model_slash_pack_count_is_separate_from_number(label):
    d = document()
    d['effective']['skus'][0]['combination'] = {'型号': label}
    d['effective']['skus'][0]['name'] = known(label)
    rows, _, _, _, _ = exported([d])
    assert rows[0]['SKU编号'] == 'JG16-12'
    assert rows[0]['销售规格'] == label


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2HCQAAAAASUVORK5CYII=')
JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAUACgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwChRRRXsHlhRRRQAUUUUAFFFFABRRRQAUUUUAf/2Q==')


def test_main_image_is_embedded_once_at_b2_and_missing_images_have_no_placeholder():
    from app.excel_export import excel_bytes
    with zipfile.ZipFile(io.BytesIO(excel_bytes([document()], {'001234': PNG}))) as z:
        media = [n for n in z.namelist() if n.startswith('xl/media/')]
        assert len(media) == 1
        assert z.read(media[0]) == PNG
        drawing = ET.fromstring(z.read('xl/drawings/drawing1.xml'))
        dns = {'d': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'}
        assert len(drawing.findall('.//d:pic', dns)) == 1
        assert drawing.find('.//d:from/d:col', dns).text == '1'
        assert drawing.find('.//d:from/d:row', dns).text == '1'
        sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        rows = sheet.findall('m:sheetData/m:row', NS)
        assert float(rows[1].get('ht')) >= 90
        assert float(rows[2].get('ht')) < 90
    for documents, images in [([document()], None), ([], {'001234': PNG})]:
        with zipfile.ZipFile(io.BytesIO(excel_bytes(documents, images))) as z:
            assert not any(n.startswith('xl/media/') for n in z.namelist())
            sheet = ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
            assert sheet.find('m:drawing', NS) is None


def test_jpeg_main_image_preserves_bytes_content_type_and_aspect_ratio():
    from app.excel_export import excel_bytes
    with zipfile.ZipFile(io.BytesIO(excel_bytes([document()], {'001234': JPEG}))) as z:
        media = [n for n in z.namelist() if n.startswith('xl/media/')]
        assert media == ['xl/media/main-image.jpeg']
        assert z.read(media[0]) == JPEG
        content_types = ET.fromstring(z.read('[Content_Types].xml'))
        assert any(e.get('Extension') == 'jpeg' and e.get('ContentType') == 'image/jpeg'
                   for e in content_types)
        drawing = ET.fromstring(z.read('xl/drawings/drawing1.xml'))
        dns = {'d': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'}
        extent = drawing.find('.//d:ext', dns)
        assert int(extent.get('cx')) == 2 * int(extent.get('cy'))


def test_unsupported_main_image_is_rejected_instead_of_embedding_invalid_bytes():
    from app.excel_export import excel_bytes
    with pytest.raises(ValueError, match='PNG.*JPEG'):
        excel_bytes([document()], {'001234': b'not an image'})


def test_opc_parts_use_default_namespaces_for_strict_excel_importers():
    from app.excel_export import excel_bytes
    with zipfile.ZipFile(io.BytesIO(excel_bytes([document()], {'001234': JPEG}))) as z:
        for name, root in [('[Content_Types].xml', b'Types'),
                           ('xl/drawings/_rels/drawing1.xml.rels', b'Relationships')]:
            assert b'<' + root + b' xmlns=' in z.read(name)
