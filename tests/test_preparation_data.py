from types import SimpleNamespace
from datetime import datetime
import pytest
from app.preparation import build_preparation, apply_overrides, evaluate_completeness, field_value
from app.taobao.parser import ParsedProduct


def product():
    return SimpleNamespace(local_product_id='local', source_product_id='123', source_platform='taobao',
        source_url='https://item.taobao.com/item.htm?id=123', collected_at=datetime(2026, 10, 5),
        current_data={}, raw_data={}, title='商品', attributes={}, assets=[], category=None, price=None)


def test_unknown_stock_and_buyer_price_are_separate():
    p = ParsedProduct('123', product().source_url, '商品', price='12.00',
        skus=[{'source_sku_id': 'sku1', 'properties': {'颜色': '蓝'}, 'stock': 0, 'price': '12.00'}],
        attributes={'发货地': '上海', '安装方式': '螺丝'})
    d = build_preparation(product(), p)
    assert d['collected']['fields']['stock']['value'] is None
    assert d['collected']['fields']['price']['value'] is None
    assert d['buyer_display']['price'] == '12.00'
    assert d['collected']['skus'][0]['buyer_display']['stock'] == 0
    assert '发货地' not in str(d['collected'])
    assert evaluate_completeness(d)['complete'] is False


def test_overrides_preserve_raw_values_and_stable_sku_identity():
    p = ParsedProduct('123', product().source_url, '原标题', skus=[{'source_sku_id': 'sku1', 'properties': {'颜色': '蓝'}}])
    d = build_preparation(product(), p)
    key = d['collected']['skus'][0]['local_sku_id']
    updated = apply_overrides(d, {'fields.title': field_value('手工标题', source='manual'),
                                  f'skus.{key}.stock': field_value(0, source='manual')})
    assert updated['collected']['fields']['title']['value'] == '原标题'
    assert updated['effective']['fields']['title']['value'] == '手工标题'
    assert updated['effective']['skus'][0]['stock']['value'] == 0
    assert d['manual_overrides'] == {}
    with pytest.raises(ValueError):
        apply_overrides(d, {'fields.freight_template': field_value('包邮')})


@pytest.mark.parametrize('status', ['not_set', 'not_applicable', 'not_obtained', 'failed'])
def test_explicit_states_remain_distinct(status):
    v = field_value(None, status=status, reason='有依据的状态')
    assert v['status'] == status
    assert v['value'] is None


def test_explicit_numeric_attribute_units_and_invalid_empty_obtained():
    p=ParsedProduct('123',product().source_url,'商品',attributes={'额定电流':'16 A','耐温等级':'-40~85℃','型号':'AB16A'})
    d=build_preparation(product(),p)
    attrs={a['source_name']:a for a in d['collected']['attributes']}
    assert attrs['额定电流']['raw_value']=='16 A'
    assert attrs['额定电流']['field']['value']=='16' and attrs['额定电流']['field']['unit']=='A'
    assert attrs['耐温等级']['field']['value']=='-40~85℃'
    assert attrs['型号']['field']['value']=='AB16A'
    with pytest.raises(ValueError):
        apply_overrides(d,{'fields.title':{'status':'obtained','value':None}})


def test_nested_exclusions_and_orphan_manual_sku_are_retained_safely():
    p=product();p.attributes={'service':{'shipping_address':'上海','warranty':'一年'}}
    d=build_preparation(p)
    assert 'shipping_address' not in str(d['collected'])
    assert p.attributes['service']['shipping_address']=='上海'
    a=ParsedProduct('123',p.source_url,'商品',skus=[{'source_sku_id':'A','properties':{'颜色':'蓝'}}])
    old=build_preparation(p,a);key=old['collected']['skus'][0]['local_sku_id']
    manual={f'skus.{key}.stock':field_value(3,source='manual')}
    newer=build_preparation(p,ParsedProduct('123',p.source_url,'商品',skus=[{'source_sku_id':'B','properties':{'颜色':'白'}}]))
    newer['manual_overrides']=manual
    merged=apply_overrides(newer,{})
    assert merged['manual_overrides']==manual and merged['issues']
    assert merged['effective']['skus'][0]['stock']['value'] == 9999
    assert merged['effective']['skus'][0]['stock']['source_refs'] == ['default_configuration']


def test_legacy_required_connector_check_and_other_categories_do_not_inherit_it():
    p=product();p.assets=[__import__('types').SimpleNamespace(id='a',source_url='https://img.alicdn.com/a.png',original_asset_id=None,
        relative_path='a.png',sha256='digest',byte_size=8,width=8,height=8,role='main')]
    parsed=ParsedProduct('123',p.source_url,'商品',category='接线端子',category_data={'name':'接线端子','path':['电子','接线端子']},
        attributes={'安装方式':'螺丝','导体材质':'铜'},images=[{'url':p.assets[0].source_url,'role':'main'}],detail_blocks=[{'type':'text','text':'原商品说明'}])
    historical = build_preparation(p, parsed)
    historical.pop('collection_profile')
    d=apply_overrides(historical,{'fields.price':field_value('12.00',source='manual'),
        'fields.stock':field_value(0,source='manual'),'fields.listing_history':field_value({'sale_status':'on_sale','mode':'original_saved'},source='manual')})
    assert d['completeness']['complete']
    pending=apply_overrides(d,{'attributes.导体材质':field_value('铜',source='manual',needs_confirmation=True)})
    assert not pending['completeness']['complete'] and pending['completeness']['pending']
    parsed.category_data={'name':'文具','path':['文具']}
    other=build_preparation(p,parsed)
    assert other['category_rules']['required_attributes']==[] and not other['completeness']['complete']
