"""Fill an artifact-authored Excel template using only Python's standard library.

The preparation document, rather than the compact package JSON, is the source
so product attributes and explicit field states survive export.
"""
import io
import re
import struct
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
from app.product_model import product_model


_MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_NS = {'m': _MAIN}
_TEMPLATE = Path(__file__).with_name('assets') / 'excel-template.xlsx'
_NUMERIC_COLUMNS = {7, 8, 12, 13}
_FIXED = ('放入仓库', '48小时', '大陆及港澳台  单一发货地  浙江温州', '使用物流配送', '包邮')
_SINGLE_NUMBER = re.compile(r'^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$')
_XML_INVALID = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')
_MODEL = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{1,12}[-_]?\d+(?:[.]\d+)*(?:[-_/－–][A-Za-z0-9.]+)*)(?![A-Za-z0-9])')
_PACK_SUFFIX = re.compile(r'[/／]\s*\d+(?:\s*[-~～至/／]\s*\d+)?\s*(?:只|个|件|片|支|枚|颗|粒|条|根|套|对)(?:装)?(?:\s*[/／]\s*包)?(?=[/／\s)）\]】]|$)')
_DRAWING = 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing'
_PACKAGE_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
_CONTENT_TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'


def _value(field):
    return field.get('value') if isinstance(field, dict) and field.get('status') == 'obtained' else None


def _number(value):
    """Return a numeric scalar only when the source unambiguously is one."""
    if isinstance(value, bool) or value is None:
        return None
    raw = str(value).strip()
    if not _SINGLE_NUMBER.fullmatch(raw):
        return None
    try:
        number = Decimal(raw)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _attribute(attributes, *names):
    for name in names:
        for attr in attributes:
            if attr.get('source_name') == name:
                value = _value(attr.get('field'))
                if value is not None:
                    unit = (attr.get('field') or {}).get('unit')
                    return f'{value} {unit}' if unit else value
    return None


def _sku_attribute(sku, attributes, *names):
    combination = sku.get('combination') or {}
    for name in names:
        value = combination.get(name)
        if value is not None and value != '':
            return value
    return _attribute(attributes, *names)


def _product_price(document, fields):
    field = fields.get('price') or {}
    if field.get('status') in {'not_set', 'failed', 'not_applicable'}:
        return None
    actual = _value(field)
    if actual is not None:
        return actual
    # An explicit manual empty state must not be repopulated from buyer data.
    if isinstance((document.get('manual_overrides') or {}).get('fields.price'), dict) or 'manual' in (field.get('source_refs') or []):
        return None
    display = document.get('buyer_display') or {}
    # A parsed minimum accompanied by a range label is not a single price.
    raw = display.get('price_raw')
    if raw and re.search(r'[-~～至]|起', str(raw)):
        return None
    value = display.get('price')
    return value if _number(value) is not None else None


def _sku_number(sku):
    combination = sku.get('combination') or {}
    for name in ('型号', '货号', '编号', 'SKU编号'):
        value = combination.get(name)
        if value is not None and value != '':
            models = set(_MODEL.findall(_PACK_SUFFIX.sub('', str(value))))
            if len(models) > 1:
                return None
            return next(iter(models)) if models else str(value)
    models = set(_MODEL.findall(_PACK_SUFFIX.sub('', str(_value(sku.get('name')) or ''))))
    return next(iter(models)) if len(models) == 1 else None


def _rows(documents):
    for document in documents:
        effective = document.get('effective') or document.get('collected') or {}
        fields = effective.get('fields') or {}
        attributes = effective.get('attributes') or []
        price = _product_price(document, fields)
        stock_field = fields.get('stock') or {}
        stock = None if 'default_configuration' in (stock_field.get('source_refs') or []) else _value(stock_field)
        url = document.get('source_url') or next((source.get('url') for source in document.get('sources', [])
                                                if source.get('url')), None)
        model = _attribute(attributes, '型号', '货号')
        model_field = next((attribute.get('field') or {} for name in ('型号', '货号') for attribute in attributes
                            if attribute.get('source_name') == name and _value(attribute.get('field')) is not None), {})
        model = product_model(model, _value(fields.get('title')), manual='manual' in (model_field.get('source_refs') or []))
        product_values = [
            _value(fields.get('title')), None, _attribute(attributes, '安装方式'),
            _attribute(attributes, '导线材质', '导体材质'), model,
        ]
        for index, sku in enumerate(effective.get('skus') or [{}]):
            yield [*product_values,
                   _value(sku.get('name')), _sku_number(sku), _value(sku.get('package_price')),
                   _value(sku.get('package_quantity')), _value(sku.get('package_unit')),
                   _sku_attribute(sku, attributes, '导线截面积', '导线截面', '标称截面积'),
                   _sku_attribute(sku, attributes, '适用线缆类型', '适用电缆类型'),
                   price, stock, *_FIXED, url if index == 0 else None]


def _image_info(image):
    """Read dimensions without a runtime imaging dependency or image conversion."""
    if not isinstance(image, bytes):
        raise ValueError('商品主图必须是PNG或JPEG图像字节')
    if image.startswith(b'\x89PNG\r\n\x1a\n') and len(image) >= 24:
        width, height = struct.unpack('>II', image[16:24])
        if width and height:
            return 'png', 'image/png', width, height
    elif image.startswith(b'\xff\xd8'):
        position = 2
        while position + 4 <= len(image):
            if image[position] != 0xFF:
                break
            while position < len(image) and image[position] == 0xFF:
                position += 1
            if position >= len(image):
                break
            marker = image[position]
            position += 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(image):
                break
            length = int.from_bytes(image[position:position + 2], 'big')
            if length < 2 or position + length > len(image):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
                height, width = struct.unpack('>HH', image[position + 3:position + 7])
                if width and height:
                    return 'jpeg', 'image/jpeg', width, height
            position += length
    raise ValueError('商品主图必须是有效的PNG或JPEG图像')


def _image_parts(template, sheet, image):
    """Retain the artifact-authored drawing, replacing only its source image."""
    parts = {}
    omitted = {name for name in template.namelist() if name.startswith('xl/media/')}
    types = ET.fromstring(template.read('[Content_Types].xml'))
    if image is None:
        drawing = sheet.find('m:drawing', _NS)
        if drawing is not None:
            sheet.remove(drawing)
        omitted.update(name for name in template.namelist() if name.startswith('xl/drawings/'))
        rels = ET.fromstring(template.read('xl/worksheets/_rels/sheet1.xml.rels'))
        for relationship in list(rels):
            if relationship.get('Type', '').endswith('/drawing'):
                rels.remove(relationship)
        parts['xl/worksheets/_rels/sheet1.xml.rels'] = _xml_bytes(rels)
        for content_type in list(types):
            if content_type.get('PartName', '').startswith('/xl/drawings/'):
                types.remove(content_type)
    else:
        extension, mime, width, height = _image_info(image)
        media_name = 'xl/media/main-image.' + extension
        parts[media_name] = image
        rels = ET.fromstring(template.read('xl/drawings/_rels/drawing1.xml.rels'))
        rels.find(f'{{{_PACKAGE_REL}}}Relationship').set('Target', '/' + media_name)
        parts['xl/drawings/_rels/drawing1.xml.rels'] = _xml_bytes(rels)
        drawing = ET.fromstring(template.read('xl/drawings/drawing1.xml'))
        scale = 112 / max(width, height)
        drawing.find(f'.//{{{_DRAWING}}}ext').attrib.update(
            cx=str(round(width * scale * 9525)), cy=str(round(height * scale * 9525)))
        parts['xl/drawings/drawing1.xml'] = _xml_bytes(drawing)
        if not any(t.get('Extension') == extension for t in types):
            ET.SubElement(types, f'{{{_CONTENT_TYPES}}}Default', Extension=extension, ContentType=mime)
    parts['[Content_Types].xml'] = _xml_bytes(types)
    return parts, omitted


def _xml_bytes(element):
    payload = ET.tostring(element, encoding='utf-8', xml_declaration=True)
    if element.tag.startswith((f'{{{_CONTENT_TYPES}}}', f'{{{_PACKAGE_REL}}}')):
        # Strict OPC readers require unprefixed Types/Relationships roots. Keep
        # this local: ElementTree's global namespace registry races in requests.
        prefix = re.search(rb'<([A-Za-z_][\w.-]*):(?:Types|Relationships)(?=[\s>])', payload)
        if prefix:
            name = prefix.group(1)
            payload = payload.replace(b'xmlns:' + name + b'=', b'xmlns=')
            payload = re.sub(rb'(</?)' + re.escape(name) + rb':', rb'\1', payload)
    return payload


def excel_bytes(documents: list[dict], main_images: dict[str, bytes] | None = None) -> bytes:
    """Export zero or one product, with one row per SKU or SKU-less product.

    Unknown values remain empty cells. Package prices are never divided and
    product inventory is never inferred from SKU inventory. String values,
    including formula-like input, are always explicit OOXML string cells.
    """
    if len(documents) > 1:
        raise ValueError('每个Excel文件只支持单个商品，请为多个商品分别导出')
    image = (main_images or {}).get(documents[0].get('source_product_id')) if documents else None
    with zipfile.ZipFile(_TEMPLATE) as template:
        sheet = ET.fromstring(template.read('xl/worksheets/sheet1.xml'))
        data = sheet.find('m:sheetData', _NS)
        _, body = list(data)
        styles = [cell.get('s', '0') for cell in body]
        data.remove(body)
        row_count = 1
        for row_count, values in enumerate(_rows(documents), start=2):
            row = ET.SubElement(data, f'{{{_MAIN}}}row', {
                'r': str(row_count), 'ht': body.get('ht', '30'), 'customHeight': '1'})
            for index, value in enumerate(values):
                column = chr(ord('A') + index)
                cell = ET.SubElement(row, f'{{{_MAIN}}}c', {'r': f'{column}{row_count}', 's': styles[index]})
                if value is None or value == '':
                    continue
                numeric = _number(value) if index in _NUMERIC_COLUMNS else None
                if numeric is not None:
                    ET.SubElement(cell, f'{{{_MAIN}}}v').text = format(numeric, 'f')
                else:
                    cell.set('t', 'inlineStr')
                    text = ET.SubElement(ET.SubElement(cell, f'{{{_MAIN}}}is'), f'{{{_MAIN}}}t')
                    text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    text.text = _XML_INVALID.sub('', str(value))
            # Increase only long description rows so wrapped labels stay readable.
            longest = max((len(str(v)) for v in (values[0], values[5]) if v is not None), default=0)
            if longest > 30:
                row.set('ht', str(min(80, 15 * ((longest + 14) // 15))))
            if row_count == 2:
                row.set('ht', '90')
        ref = f'A1:T{row_count}'
        dimension = sheet.find('m:dimension', _NS)
        if dimension is None:
            dimension = ET.Element(f'{{{_MAIN}}}dimension')
            sheet.insert(1 if sheet.find('m:sheetPr', _NS) is not None else 0, dimension)
        dimension.set('ref', ref)
        table = ET.fromstring(template.read('xl/tables/table1.xml'))
        table.set('ref', ref)
        table.find('m:autoFilter', _NS).set('ref', ref)
        parts, omitted = _image_parts(template, sheet, image)
        parts['xl/worksheets/sheet1.xml'] = _xml_bytes(sheet)
        parts['xl/tables/table1.xml'] = _xml_bytes(table)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for item in template.infolist():
                if item.filename in omitted:
                    continue
                payload = parts.pop(item.filename, None)
                if payload is None:
                    payload = template.read(item.filename)
                archive.writestr(item.filename, payload)
            for name, payload in parts.items():
                archive.writestr(name, payload)
        return output.getvalue()
