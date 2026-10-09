"""Consumer exports, separate from the immutable preparation/editing schema."""
import copy
from collections import defaultdict

from app.preparation import FIELD_LABELS, SKU_FIELDS, FOCUSED_ATTRIBUTES
from app.sku_package import PACKAGE_SKU_FIELDS, PACKAGE_LABELS

EXPLICIT_STATES = {'not_set', 'not_applicable', 'failed'}


def _known(field):
    return field.get('status') == 'obtained' and field.get('value') is not None and field.get('value') != ''


def _display_values(display):
    values = {}
    for key in ('price', 'stock'):
        value = display.get(key)
        if value is not None and value != '':
            values['display_' + key] = copy.deepcopy(value)
    raw = display.get('price_raw')
    if 'display_price' in values and isinstance(raw, dict) and raw.get('priceTitle'):
        values['display_price_type'] = raw['priceTitle']
    return values


def _field_states(fields):
    return {key: {'status': field['status'], 'reason': field.get('reason')}
            for key, field in fields.items() if field.get('status') in EXPLICIT_STATES}


def _state_text(states):
    labels = {'not_set': '明确未设置', 'not_applicable': '不适用', 'failed': '获取失败'}
    return '；'.join(f"{PACKAGE_LABELS.get(key, FIELD_LABELS.get(key.split('.', 1)[-1], key))}：{labels[state['status']]}"
                    + (f"（{state['reason']}）" if state.get('reason') else '') for key, state in states.items())


def build_export(document, included_assets=None):
    """Only a package's verified copied files may have local paths in an export."""
    doc = document
    effective = doc['effective']
    included = included_assets or {}
    mode = 'metadata_only' if included_assets is None else 'package'
    fields = {k: copy.deepcopy(f['value']) for k, f in effective['fields'].items() if _known(f)}
    attributes = {a['source_name']: copy.deepcopy(a['field']['value'])
                  for a in effective['attributes'] if _known(a['field'])}
    units = {a['source_name']: a['field']['unit'] for a in effective['attributes']
             if _known(a['field']) and a['field'].get('unit')}
    skus, unavailable = [], defaultdict(list)
    for sku in effective['skus']:
        row = {k: sku.get(k) for k in ('local_sku_id', 'source_sku_id', 'order') if sku.get(k) is not None}
        row['specifications'] = copy.deepcopy(sku['combination'])
        for key in sorted(SKU_FIELDS):
            if _known(sku[key]):
                row[key] = copy.deepcopy(sku[key]['value'])
            elif sku[key]['status'] not in {'not_set', 'not_applicable'}:
                unavailable[key].append(sku['order'])
        states = _field_states({key: sku[key] for key in sorted(SKU_FIELDS)})
        if states:
            row['field_states'] = states
        row.update(_display_values(sku.get('buyer_display') or {}))
        pending = [key for key in sorted(SKU_FIELDS) if sku[key].get('needs_confirmation')]
        if pending:
            row['needs_confirmation'] = pending
        if sku.get('image_source_url'):
            row['image_source_url'] = sku['image_source_url']
        relative = included.get(sku.get('image_asset_id'))
        if relative:
            row['image_relative_path'] = relative
        skus.append(row)
    media = []
    for item in doc['media']:
        row = {k: copy.deepcopy(item[k]) for k in ('role', 'order', 'is_first', 'source_url', 'width', 'height')
               if item.get(k) is not None}
        relative = included.get(item.get('asset_id'))
        row['file_included'] = bool(relative)
        if item.get('needs_confirmation'):
            row['needs_confirmation'] = True
        if relative:
            row['relative_path'] = relative
        elif item.get('status') != 'obtained':
            row['error'] = item.get('reason') or '素材未取得'
        media.append(row)
    blocks = []
    for item in doc['detail_blocks']:
        row = {k: copy.deepcopy(item[k]) for k in ('type', 'text', 'order', 'url') if item.get(k) is not None}
        relative = included.get(item.get('asset_id'))
        if relative:
            row['relative_path'] = relative
        blocks.append(row)
    output = {k: copy.deepcopy(doc[k]) for k in
              ('local_product_id', 'source_product_id', 'source_platform', 'captured_at')}
    source = next((s for s in doc['sources'] if s.get('url')), {})
    output.update(schema_version='2.0', export_mode=mode, source_url=source.get('url'),
                  fields=fields, attributes=attributes, skus=skus, media=media, detail_blocks=blocks)
    output.update(_display_values(doc.get('buyer_display') or {}))
    if units:
        output['attribute_units'] = units
    states = _field_states({'fields.' + k: f for k, f in effective['fields'].items()})
    states.update(_field_states({'attributes.' + a['source_name']: a['field'] for a in effective['attributes']}))
    if states:
        output['field_states'] = states
    output['collection_summary'] = {
        'sku_count': len(skus), 'media_count': len(media),
        'missing_fields': [k for k, f in effective['fields'].items()
                           if not _known(f) and f['status'] not in {'not_set', 'not_applicable'}],
        'unavailable_sku_fields': dict(unavailable),
        'preparation_complete': doc['completeness']['complete'],
        'pending_fields': [k for k, f in effective['fields'].items() if f.get('needs_confirmation')],
        'pending_attributes': [a['source_name'] for a in effective['attributes'] if a['field'].get('needs_confirmation')],
        'notes': ['展示价格和展示库存来自公开页面，未核实为后台实际值。',
                  '未知值省略，不代表零、不适用或未设置。',
                  '本文件仅含资料；下载完整资料 ZIP 获取本地素材。' if mode == 'metadata_only'
                  else '移动或复制资料时请保留整个资料包及 assets 文件夹。']}
    if doc.get('collection_profile') == 'sku_package':
        output['collection_profile'] = 'sku_package'
        output['fields'] = {k: v for k, v in fields.items() if k in {'title', 'price', 'stock'}}
        output['attributes'] = {k: v for k, v in attributes.items() if k in FOCUSED_ATTRIBUTES}
        if 'attribute_units' in output:
            output['attribute_units'] = {k: v for k, v in units.items() if k in output['attributes']}
        unavailable = defaultdict(list)
        for sku, row in zip(effective['skus'], skus):
            for key in ('display_price', 'display_price_type', 'display_stock', *SKU_FIELDS):
                row.pop(key, None)
            for key in sorted(PACKAGE_SKU_FIELDS | {'stock'}):
                field = sku.get(key) or {}
                if _known(field):
                    row[key] = copy.deepcopy(field['value'])
                else:
                    unavailable[key].append(sku['order'])
            refs = sku['stock'].get('source_refs') or []
            row['stock_source'] = refs[0] if refs else None
            row['needs_confirmation'] = [key for key in sorted(PACKAGE_SKU_FIELDS | {'stock'}) if (sku.get(key) or {}).get('needs_confirmation')]
            if not row['needs_confirmation']:
                row.pop('needs_confirmation')
            states = _field_states({key: sku.get(key) or {} for key in PACKAGE_SKU_FIELDS | {'stock'}})
            row.pop('field_states', None)
            if states:
                row['field_states'] = states
        for key in ('display_price', 'display_price_type', 'display_stock'):
            output.pop(key, None)
        focused_states = {k: v for k, v in output.get('field_states', {}).items()
                          if k in {'fields.title', 'fields.price', 'fields.stock'} or k.removeprefix('attributes.') in FOCUSED_ATTRIBUTES}
        output.pop('field_states', None)
        if focused_states:
            output['field_states'] = focused_states
        output['collection_summary'].update(
            unavailable_sku_fields=dict(unavailable),
            missing_fields=['title'] if not _known(effective['fields']['title']) else [],
            missing_attributes=[] if output['attributes'] else ['型号'],
            pending_fields=['title'] if effective['fields']['title'].get('needs_confirmation') else [],
            pending_attributes=[a['source_name'] for a in effective['attributes'] if a['source_name'] in {'型号', '货号'} and a['field'].get('needs_confirmation')],
            notes=['包装数量来自SKU名称或规格中的明确数量；每包价格保留页面该SKU价格，不换算单件价格。',
                   '库存9999是后续填写的默认值，不代表来源商品库存。',
                   '未取得的包装数量和每包价格留空，可人工补充。'])
    return output


def export_provenance(document):
    """Keep origins/explicit non-values and edits outside the practical data file."""
    effective = document['effective']
    metadata = {}
    for key, field in effective['fields'].items():
        if field['status'] != 'not_obtained':
            metadata['fields.' + key] = copy.deepcopy(field)
    for attr in effective['attributes']:
        if attr['field']['status'] != 'not_obtained':
            metadata['attributes.' + attr['source_name']] = {
                **copy.deepcopy(attr['field']), 'raw_value': copy.deepcopy(attr.get('raw_value'))}
    for sku in effective['skus']:
        for key in sorted(SKU_FIELDS | PACKAGE_SKU_FIELDS):
            if key in sku and sku[key]['status'] != 'not_obtained':
                metadata['skus.' + sku['local_sku_id'] + '.' + key] = copy.deepcopy(sku[key])
    stock_evidence = []
    for sku in document['collected']['skus']:
        record = {'local_sku_id': sku['local_sku_id'], 'source_sku_id': sku.get('source_sku_id')}
        public = (sku.get('buyer_display') or {}).get('stock')
        if public is not None:
            record['buyer_display_stock'] = copy.deepcopy(public)
        if sku['stock'].get('source_refs') and 'default_configuration' not in sku['stock']['source_refs']:
            record['recorded_stock'] = copy.deepcopy(sku['stock'])
        if len(record) > 2:
            stock_evidence.append(record)
    return {'capture_id': document.get('capture_id'),
            'manual_revision_id': document.get('manual_revision_id'),
            'manual_overrides': copy.deepcopy(document.get('manual_overrides', {})),
            'field_evidence': metadata, 'source_stock_evidence': stock_evidence}


def product_csv_row(data):
    row = {'来源商品ID': data['source_product_id'], '来源链接': data['source_url'], '采集时间': data['captured_at']}
    row.update({FIELD_LABELS.get(k, k): v for k, v in data['fields'].items()})
    # Prefix properties to avoid collisions with product fields or other metadata.
    row.update({'属性-' + k: v for k, v in data['attributes'].items()})
    row.update({'单位-' + k: v for k, v in data.get('attribute_units', {}).items()})
    for key, label in (('display_price', '展示价格'), ('display_price_type', '展示价格类型'), ('display_stock', '展示库存')):
        if key in data:
            row[label] = data[key]
    pending = [FIELD_LABELS.get(k, k) for k in data['collection_summary']['pending_fields']]
    pending.extend(data['collection_summary']['pending_attributes'])
    if pending:
        row['待确认字段'] = '、'.join(pending)
    if data.get('field_states'):
        row['状态说明'] = _state_text(data['field_states'])
    return row


SKU_COLUMNS = {'source_sku_id': '来源SKU_ID', 'order': '顺序', 'display_price': '展示价格',
    'display_price_type': '展示价格类型', 'display_stock': '展示库存', 'price': '记录价格',
    'stock': '记录库存', 'currency': '币种', 'stock_unit': '库存单位', 'merchant_code': '商家编码',
    'barcode': '条形码', 'sale_status': '销售状态', 'image_relative_path': '图片文件',
    'image_source_url': '图片来源'}

PACKAGE_SKU_COLUMNS = {**PACKAGE_LABELS, 'stock': '库存', 'stock_source': '库存来源',
                       'source_sku_id': '来源SKU_ID', 'image_relative_path': '图片文件', 'image_source_url': '图片来源'}


def sku_csv_row(sku):
    if 'stock_source' in sku:
        values = {label: sku.get(key) for key, label in PACKAGE_SKU_COLUMNS.items()}
        if sku.get('stock_source') == 'default_configuration':
            values['库存来源'] = '默认填写'
        return {**values,
                '待确认字段': '、'.join(PACKAGE_LABELS.get(key, key) for key in sku.get('needs_confirmation', [])),
                '状态说明': _state_text(sku.get('field_states', {}))}
    return {'规格': ' / '.join(f'{k}: {v}' for k, v in sku['specifications'].items()),
            '待确认字段': '、'.join(sku.get('needs_confirmation', [])),
            '状态说明': _state_text(sku.get('field_states', {})),
            **{label: sku.get(key) for key, label in SKU_COLUMNS.items()}}
