"""Conservative boundary for supplied saved records; no guessed seller API or writes."""
import copy
from datetime import datetime
from urllib.parse import urlparse
from app.preparation import FIELD_LABELS, SKU_FIELDS, field_value, validate_value, apply_overrides, allowed_name


def read_capabilities():
    return {'seller_backend': {'verified': False, 'status': 'not_obtained',
        'reason': '未取得可读取的原商品后台页面；当前浏览器连接枚举失败。请人工登录/验证后再核实。'},
        'official_export': {'verified': False, 'reason': '当前账号导出入口及文件格式尚未核实；不能声称支持平台导入/导出格式'},
        'saved_record': {'supported': True, 'reason': '可补充按来源商品/SKU ID 整理的已保存商品记录；导入值仍待人工确认'},
        'ocr': {'available': False, 'reason': '未发现已配置的本地 OCR 工具；可注明原图位置人工录入参数'}}


def merge_saved_seller_data(document: dict, source: dict) -> dict:
    if str(source.get('source_product_id')) != document['source_product_id']:
        raise ValueError('来源商品 ID 不一致，不能按标题匹配')
    if source.get('record_kind') != 'saved_product':
        raise ValueError('仅接受原商品已保存记录，不接受新建表单或默认值')
    url = urlparse(str(source.get('source_url', '')))
    host = url.hostname or ''
    if url.scheme != 'https' or not (host.endswith('.taobao.com') or host.endswith('.tmall.com')) or url.username:
        raise ValueError('请提供可追溯的淘宝/天猫原商品资料来源链接')
    try:
        datetime.fromisoformat(source['observed_at'].replace('Z', '+00:00'))
    except (KeyError, ValueError, AttributeError):
        raise ValueError('须提供实际资料读取时间') from None
    doc = copy.deepcopy(document)
    source_id = 'saved_record_' + str(len(doc['sources']) + 1)
    doc['sources'].append({'id': source_id, 'source_product_id': source['source_product_id'],
        'url': source['source_url'], 'observed_at': source['observed_at'], 'method':'user_supplied_saved_record',
        'verified_on_real_page': False})
    def merge(target, key, raw, path):
        val = ({**field_value(None), **copy.deepcopy(raw)} if isinstance(raw, dict) and 'status' in raw else field_value(raw))
        val.update(source_refs=[source_id], observed_at=source['observed_at'], needs_confirmation=True)
        validate_value(key, val)
        old = target[key]
        if old.get('value') != val.get('value') and old.get('value') is not None:
            doc['issues'].append({'field': path, 'reason': '来源值不同，保留差异待确认', 'previous': old, 'incoming': val})
        target[key] = val
    for key, value in source.get('fields', {}).items():
        if key not in FIELD_LABELS:
            raise ValueError('不支持或属于排除范围的商品字段：' + str(key))
        merge(doc['collected']['fields'], key, value, 'fields.' + key)
    for name, value in source.get('attributes', {}).items():
        if not allowed_name(name):
            raise ValueError('不支持或属于排除范围的属性：' + str(name))
        attr = next((a for a in doc['collected']['attributes'] if a['source_name']==name), None)
        if attr is None:
            attr = {'source_name':name, 'raw_value':None, 'field':field_value(), 'normalized_value':None}
            doc['collected']['attributes'].append(attr)
        merge(attr, 'field', value, 'attributes.'+name)
        attr['raw_value'] = copy.deepcopy(attr['field']['value'])
    for incoming in source.get('skus', []):
        sku_id = str(incoming.get('source_sku_id') or '')
        found = [s for s in doc['collected']['skus'] if str(s['source_sku_id'] or '') == sku_id and sku_id]
        if len(found) != 1:
            raise ValueError('来源 SKU ID 未取得或无法唯一关联，不能按名称推测')
        sku = found[0]
        for key, value in incoming.items():
            if key == 'source_sku_id':
                continue
            if key not in SKU_FIELDS:
                raise ValueError('不支持的 SKU 字段：' + key)
            merge(sku, key, value, 'skus.' + sku['local_sku_id'] + '.' + key)
    return apply_overrides(doc, {})
