"""Versioned preparation values. Public display information never becomes seller stock."""
import copy
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from app.sku_package import DEFAULT_STOCK, PACKAGE_SKU_FIELDS, PACKAGE_LABELS, package_values

STATUSES = {'obtained', 'not_set', 'not_applicable', 'not_obtained', 'failed'}
FIELD_LABELS = {
    'title': '宝贝标题', 'guide_title': '导购标题', 'category_name': '当前类目',
    'category_path': '完整类目路径', 'category_id': '来源类目 ID', 'price': '一口价',
    'currency': '价格币种', 'stock': '总库存', 'stock_unit': '库存单位',
    'listing_history': '原商品上架设置及销售状态（来源记录）', 'purchase_notice': '购买须知',
    'merchant_code': '商家编码', 'multi_buy_discount': '多件优惠', 'product_precheck': '商品预检',
    'stock_deduction': '库存扣减方式', 'custom_specification': '自定义宝贝规格',
    'barcode': '商品条形码', 'regional_sale_limits': '区域限售', 'after_sales': '可编辑售后服务',
    'item_type': '宝贝类型', 'purchase_location': '采购地',
}
FOCUSED_ATTRIBUTES = {'安装方式', '导线材质', '导体材质', '型号', '货号', '导线截面积', '导线截面', '标称截面积', '适用线缆类型', '适用电缆类型'}
ATTRIBUTE_NAMES = ['安装方式', '导线材质', '导体材质', '型号', '导线截面积', '适用线缆类型', 'SV型号', '外壳材质', '产地', '触点镀层',
    '电源配件', '定制服务', '额定电流', '额定电压', '防护等级', '国际认证', '极数', '密封件材质',
    '耐温等级', '品牌', '行业认证', '振动等级', '阻燃等级', '最小起订量']
SKU_FIELDS = {'price', 'currency', 'stock', 'stock_unit', 'merchant_code', 'barcode', 'sale_status'}
BLOCKED = re.compile(r'发货|运费|提取方式|包邮|shipping|delivery|freight|postage|dispatch|pickup|shop.?categor|seller.?cids|3[:：]4', re.I)


def allowed_name(name):
    return isinstance(name, str) and bool(name.strip()) and not BLOCKED.search(name)


def sanitize_source_value(value):
    if isinstance(value, dict):
        return {key: sanitize_source_value(child) for key, child in value.items() if allowed_name(key)}
    if isinstance(value, list):
        return [sanitize_source_value(child) for child in value]
    return value


def field_value(value=None, *, status=None, source=None, reason=None, unit=None, needs_confirmation=False):
    status = status or ('obtained' if value is not None and value != '' else 'not_obtained')
    if status not in STATUSES:
        raise ValueError('字段状态无效')
    if status != 'obtained' and value is not None:
        raise ValueError('未取得状态的值应为空')
    return {'value': value, 'status': status, 'source_refs': [source] if source else [],
            'reason': reason, 'unit': unit, 'needs_confirmation': bool(needs_confirmation)}


def validate_value(key, value):
    if not isinstance(value, dict) or value.get('status') not in STATUSES:
        raise ValueError('请提供合法的字段状态和值')
    v = value.get('value')
    if value['status'] == 'obtained' and (v is None or v == '' or v == []):
        raise ValueError('已取得字段须提供实际值')
    if value['status'] != 'obtained' and v is not None:
        raise ValueError('未知或不适用的值必须为空')
    if value['status'] in {'not_set', 'not_applicable'} and not value.get('reason'):
        raise ValueError('明确未设置和不适用须说明依据')
    if key in {'stock', 'package_quantity'} and v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < (1 if key == 'package_quantity' else 0)):
        raise ValueError('包装数量须为正整数；库存须为非负整数，未知请使用未获取状态')
    if key in {'price', 'package_price'} and v is not None:
        try:
            price = Decimal(str(v))
            if not price.is_finite() or price < 0:
                raise ValueError()
            value['value'] = format(price, 'f')
        except (InvalidOperation, ValueError):
            raise ValueError('价格必须为非负有限数值') from None
    if key == 'category_path' and v is not None and (not isinstance(v, list) or not all(isinstance(x, str) and x for x in v)):
        raise ValueError('类目路径须为从上到下的名称列表')
    # Excluded settings cannot be hidden inside a compound field.
    def check(obj):
        if isinstance(obj, dict):
            for name, child in obj.items():
                if not allowed_name(name):
                    raise ValueError('不接受本次排除的物流或店铺分类字段')
                check(child)
        elif isinstance(obj, list):
            for child in obj:
                check(child)
    check(v)


def build_preparation(product, parsed=None, aliases=None):
    current = product.current_data or {}
    raw = parsed.raw_data if parsed else product.raw_data or {}
    source = 'public_product_page' if parsed else 'legacy_unverified'
    title = parsed.title if parsed else product.title
    category = parsed.category_data if parsed else current.get('category_data', {})
    category = category or {'name': parsed.category if parsed else product.category}
    fields = {key: field_value(reason='公开页面不能证明原商品实际设置；待后台资料或人工补充') for key in FIELD_LABELS}
    fields['title'] = field_value(title, source=source)
    fields['category_name'] = field_value(category.get('name'), source=source)
    fields['category_path'] = field_value(category.get('path') or None, source=source,
                                          reason='完整路径未取得' if not category.get('path') else None)
    fields['category_id'] = field_value(category.get('source_category_id'), source=source)
    attrs = dict(parsed.attributes if parsed else product.attributes or {})
    model = parsed.model if parsed else getattr(product, 'model', None)
    if model and not (attrs.get('型号') or attrs.get('货号')):
        attrs['型号'] = model
    attributes = []
    for name, value in attrs.items():
        if not allowed_name(name):
            continue
        value = sanitize_source_value(value)
        numeric, unit = value, None
        if name in {'额定电流', '额定电压', '耐温等级', '最小起订量'} and isinstance(value, str):
            match = re.fullmatch(r'\s*([+-]?\d+(?:\.\d+)?)\s*(mA|A|kV|mV|V|℃|°C|件|个|只|套)\s*', value)
            if match:
                numeric, unit = match.groups()
        attributes.append({'source_name': name, 'raw_value': value,
            'field': field_value(numeric, source=source, unit=unit), 'normalized_value': None})
    skus = []
    for index, sku in enumerate(parsed.skus if parsed else current.get('skus', [])):
        props = sanitize_source_value(sku.get('properties') or {})
        identity = str(sku.get('source_sku_id') or json.dumps(props, ensure_ascii=False, sort_keys=True))
        key = 'sku_' + hashlib.sha256((product.local_product_id + ':' + identity).encode()).hexdigest()[:20]
        skus.append({'local_sku_id': key, 'source_sku_id': sku.get('source_sku_id'), 'order': index + 1,
            'specifications': [{'name': k, 'value': v} for k, v in props.items()], 'combination': props,
            **{name: field_value(reason='买家展示信息不作为卖家实际值') for name in SKU_FIELDS},
            'image_source_url': sku.get('image_url'), 'image_asset_id': None,
            'buyer_display': {'price': sku.get('price'), 'price_raw': sku.get('price_raw'), 'stock': sku.get('stock')}})
        values = package_values(sku.get('name'), props)
        values['package_price'] = sku.get('package_price', sku.get('price'))
        skus[-1].update({name: field_value(value, source=source) for name, value in values.items()})
        skus[-1]['stock'] = field_value(DEFAULT_STOCK, source='default_configuration', reason='后续填写的默认库存，不是采集库存')
    assets = {a.source_url: a for a in product.assets if a.original_asset_id is None}
    assets.update(aliases or {})
    images = parsed.images if parsed else raw.get('images') or [
        {'url': a.source_url, 'role': a.role} for a in product.assets if a.original_asset_id is None]
    media, orders = [], {}
    roles = {'main', 'detail', 'sku', 'white_background', 'selling_point', 'product_video', 'after_purchase_video'}
    for image in images:
        role = image.get('role', 'detail')
        if role not in roles:
            continue
        orders[role] = orders.get(role, 0) + 1
        asset = assets.get(image['url'])
        media.append({'role': role, 'order': orders[role], 'is_first': role == 'main' and orders[role] == 1,
            'source_url': image['url'], 'asset_id': asset.id if asset else None,
            'relative_path': f'assets/{asset.id}' + Path(asset.relative_path).suffix if asset else None,
            'sha256': asset.sha256 if asset else None, 'byte_size': asset.byte_size if asset else None,
            'width': asset.width if asset else None, 'height': asset.height if asset else None,
            'status': 'obtained' if asset else 'failed', 'reason': None if asset else '素材未下载或无法关联',
            'needs_confirmation': bool(role == 'main' and asset and asset.width != asset.height)})
    for sku in skus:
        asset = assets.get(sku['image_source_url'])
        sku['image_asset_id'] = asset.id if asset else None
        sku['image_relative_path'] = f'assets/{asset.id}' + Path(asset.relative_path).suffix if asset else None
    blocks = copy.deepcopy(parsed.detail_blocks if parsed else current.get('detail_blocks') or [])
    if not blocks:
        blocks = [{'type': 'image', 'url': x['source_url'], 'order': x['order']} for x in media if x['role'] == 'detail']
    for block in blocks:
        if block['type'] == 'image':
            asset = assets.get(block.get('url'))
            block['asset_id'] = asset.id if asset else None
            block['relative_path'] = f'assets/{asset.id}' + Path(asset.relative_path).suffix if asset else None
    doc = {'schema_version': '1.0', 'collection_profile': 'sku_package', 'local_product_id': product.local_product_id,
        'source_product_id': product.source_product_id, 'source_platform': product.source_platform,
        'captured_at': product.collected_at.isoformat() + 'Z', 'capture_id': None,
        'sources': [{'id': source, 'url': product.source_url, 'source_product_id': product.source_product_id,
                     'observed_at': product.collected_at.isoformat() + 'Z', 'method': source},
                    {'id': 'default_configuration', 'url': None, 'source_product_id': product.source_product_id,
                     'observed_at': product.collected_at.isoformat() + 'Z', 'method': 'default_configuration',
                     'reason': '用户指定每个SKU后续填写库存默认9999'}],
        'collected': {'fields': fields, 'attributes': attributes, 'skus': skus}, 'manual_overrides': {},
        'media': media, 'detail_blocks': blocks, 'issues': [],
        'images': copy.deepcopy(media),  # Compatibility alias; roles/order remain explicit. New readers use media.
        'buyer_display': {'price': parsed.price if parsed else str(product.price) if product.price is not None else None,
                          'price_raw': parsed.buyer_display.get('price_raw') if parsed else None,
                          'source': source}, 'category_rules': {'verified': category.get('name') == '接线端子',
                          'required_attributes': ['安装方式', '导体材质'] if category.get('name') == '接线端子' else []},
        'reading_capabilities': {'seller': 'not_verified', 'reason': '尚未核实原商品后台或官方导出读取方式',
                                 'ocr': 'not_obtained', 'ocr_reason': '未执行图片文字识别；可人工补充并注明原图'}}
    return apply_overrides(doc, {})


def use_package_profile(document, source_skus=None):
    """Project older captures into the new view without changing saved evidence."""
    doc = copy.deepcopy(document)
    doc['collection_profile'] = 'sku_package'
    if not any(s['id'] == 'default_configuration' for s in doc['sources']):
        doc['sources'].append({'id': 'default_configuration', 'url': None, 'source_product_id': doc['source_product_id'],
                              'observed_at': doc['captured_at'], 'method': 'default_configuration',
                              'reason': '用户指定每个SKU后续填写库存默认9999'})
    originals = {str(s.get('source_sku_id')): s for s in source_skus or [] if s.get('source_sku_id')}
    for sku in doc['collected']['skus']:
        original = originals.get(str(sku.get('source_sku_id')), {})
        values = package_values(original.get('name'), sku.get('combination'))
        values['package_price'] = (sku.get('buyer_display') or {}).get('price')
        for key, value in values.items():
            sku.setdefault(key, field_value(value, source='public_product_page'))
        if sku['stock']['status'] == 'not_obtained':
            sku['stock'] = field_value(DEFAULT_STOCK, source='default_configuration', reason='后续填写的默认库存，不是采集库存')
    return doc


def apply_overrides(document, overrides):
    doc = copy.deepcopy(document)
    combined = {**doc.get('manual_overrides', {}), **overrides}
    effective = copy.deepcopy(doc['collected'])
    if doc.get('collection_profile') == 'sku_package':
        from app.product_model import product_model
        title = effective['fields']['title'].get('value')
        for attribute in effective['attributes']:
            field = attribute['field']
            if attribute['source_name'] == '型号' and field.get('status') == 'obtained':
                value = product_model(field.get('value'), title, manual='manual' in (field.get('source_refs') or []))
                if value != field.get('value'):
                    field['value'] = value
                    field['reason'] = '按用户要求保留商品系列型号，具体型号及包装信息保存在SKU规格中'
        for sku in effective['skus']:
            refs = set(sku['stock'].get('source_refs') or [])
            if not refs.intersection({'manual', 'default_configuration'}):
                sku['stock'] = field_value(DEFAULT_STOCK, source='default_configuration', reason='后续填写的默认库存，不是采集库存')
    for path, value in combined.items():
        if not isinstance(path, str):
            raise ValueError('字段路径必须为文本')
        pieces = path.split('.')
        legal = ((pieces[0]=='fields' and len(pieces)==2 and pieces[1] in FIELD_LABELS)
            or (pieces[0]=='attributes' and len(pieces)==2 and allowed_name(pieces[1]))
            or (pieces[0]=='skus' and len(pieces)==3 and pieces[2] in SKU_FIELDS | PACKAGE_SKU_FIELDS))
        if not legal:
            raise ValueError('不支持的字段路径，或字段属于排除范围')
        if value is None:
            continue  # Explicit removal of a previous override.
        validate_value(pieces[-1], value)
        if pieces[0] == 'fields' and len(pieces) == 2 and pieces[1] in FIELD_LABELS:
            effective['fields'][pieces[1]] = copy.deepcopy(value)
        elif pieces[0] == 'attributes' and len(pieces) == 2 and allowed_name(pieces[1]):
            attr = next((a for a in effective['attributes'] if a['source_name'] == pieces[1]), None)
            if attr is None:
                attr = {'source_name': pieces[1], 'raw_value': None, 'normalized_value': None}
                effective['attributes'].append(attr)
            attr['field'] = copy.deepcopy(value)
        elif pieces[0] == 'skus' and len(pieces) == 3 and pieces[2] in SKU_FIELDS | PACKAGE_SKU_FIELDS:
            candidates = [s for s in effective['skus'] if s['local_sku_id'] == pieces[1]]
            sku = candidates[0] if len(candidates) == 1 else None
            if sku is None:
                if path in overrides:
                    raise ValueError('SKU 不存在，请重新读取当前商品资料')
                doc['issues'].append({'field': path, 'reason': '来源 SKU 已不存在或无法唯一关联，保留人工修订供确认'})
            else:
                sku[pieces[2]] = copy.deepcopy(value)
        else:
            raise ValueError('不支持的字段路径，或字段属于排除范围')
    doc['manual_overrides'] = {k: v for k, v in combined.items() if v is not None}
    doc['effective'] = effective
    doc['completeness'] = evaluate_completeness(doc)
    return doc


def evaluate_completeness(doc):
    if doc.get('collection_profile') == 'sku_package':
        missing, pending = [], []
        title = doc['effective']['fields']['title']
        if title['status'] != 'obtained':
            missing.append({'field': 'fields.title', 'label': '商品名称', 'status': title['status']})
        if title.get('needs_confirmation'):
            pending.append({'field': 'fields.title', 'label': '商品名称', 'reason': '需要人工确认'})
        model = next((a['field'] for a in doc['effective']['attributes'] if a['source_name'] in {'型号', '货号'} and a['field']['status'] == 'obtained'), None)
        if not model:
            missing.append({'field': 'attributes.型号', 'label': '商品型号', 'status': 'not_obtained'})
        elif model.get('needs_confirmation'):
            pending.append({'field': 'attributes.型号', 'label': '商品型号', 'reason': '需要人工确认'})
        if not doc['effective']['skus']:
            missing.append({'field': 'skus', 'label': 'SKU信息', 'status': 'not_obtained'})
        for sku in doc['effective']['skus']:
            for key, label in {**PACKAGE_LABELS, 'stock': '填写库存'}.items():
                value = sku.get(key) or {}
                item = {'field': f"skus.{sku['local_sku_id']}.{key}", 'label': f"SKU {sku['order']} {label}", 'status': value.get('status', 'not_obtained')}
                if value.get('status') != 'obtained':
                    missing.append(item)
                elif value.get('needs_confirmation'):
                    pending.append({**item, 'reason': '需要人工确认'})
        return {'complete': not missing and not pending, 'missing': missing, 'pending': pending,
                'label': '商品名称、型号及SKU包装信息', 'excluded_logistics': True}
    fields = doc['effective']['fields']
    missing, pending = [], []
    for key in ('title', 'category_name', 'category_path', 'price', 'stock', 'listing_history'):
        value = fields[key]
        if value['status'] != 'obtained' or value['value'] is None or value['value'] == '' or value['value'] == []:
            missing.append({'field': 'fields.' + key, 'label': FIELD_LABELS[key], 'status': value['status'], 'reason': value.get('reason')})
        elif value.get('needs_confirmation'):
            pending.append({'field': 'fields.' + key, 'label': FIELD_LABELS[key], 'reason': '需要人工确认'})
    main = [m for m in doc['media'] if m['role'] == 'main']
    if not main or any(m['status'] != 'obtained' for m in main):
        missing.append({'field': 'media.main', 'label': '1:1 主图', 'status': 'not_obtained'})
    if any(m.get('needs_confirmation') for m in main):
        pending.append({'field': 'media.main', 'label': '1:1 主图', 'reason': '主图比例需确认，不会自动裁切'})
    if not doc['detail_blocks'] or any(b['type'] == 'image' and not b.get('asset_id') for b in doc['detail_blocks']):
        missing.append({'field': 'detail_blocks', 'label': '宝贝详情', 'status': 'not_obtained'})
    if not doc['category_rules']['verified']:
        pending.append({'field': 'category_rules', 'label': '类目必填要求', 'reason': '对应类目要求尚未核实'})
    for name in doc['category_rules']['required_attributes']:
        attr = next((a for a in doc['effective']['attributes'] if a['source_name'] == name), None)
        if not attr or attr['field']['status'] != 'obtained' or not attr['field'].get('value'):
            missing.append({'field': 'attributes.' + name, 'label': name, 'status': attr['field']['status'] if attr else 'not_obtained'})
        elif attr['field'].get('needs_confirmation'):
            pending.append({'field': 'attributes.' + name, 'label': name, 'reason': '需要人工确认'})
    for sku in doc['effective']['skus']:
        for name in ('price', 'stock'):
            if sku[name]['status'] != 'obtained' or sku[name].get('needs_confirmation'):
                pending.append({'field': f"skus.{sku['local_sku_id']}.{name}", 'label': f"SKU {sku['order']} {name}", 'reason': '实际值未取得或待确认'})
    return {'complete': not missing and not pending, 'missing': missing, 'pending': pending,
            'label': '采集范围内必填资料', 'excluded_logistics': True}
