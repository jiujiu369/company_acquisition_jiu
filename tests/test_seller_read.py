import pytest
from app.taobao.seller_read import merge_saved_seller_data, read_capabilities
from test_preparation_data import product
from app.preparation import build_preparation


def record():
    return {'source_product_id': '123', 'record_kind':'saved_product', 'source_url':'https://myseller.taobao.com/item/123',
            'observed_at':'2026-10-05T01:00:00Z', 'fields':{'price': '20.00','stock':0, 'currency':'CNY'}, 'skus':[]}


def test_saved_record_keeps_provenance_and_conflict():
    d = build_preparation(product()); r = record()
    updated = merge_saved_seller_data(d, r)
    assert updated['effective']['fields']['stock']['value'] == 0
    assert updated['effective']['fields']['price']['value'] == '20.00'
    assert updated['effective']['fields']['price']['needs_confirmation'] is True
    assert d['collected']['fields']['stock']['value'] is None
    assert updated['sources'][-1]['method'] == 'user_supplied_saved_record'


@pytest.mark.parametrize('key,value', [('source_product_id','456'),('record_kind','new_product_form')])
def test_wrong_product_or_new_form_rejected(key, value):
    r = record(); r[key] = value
    with pytest.raises(ValueError): merge_saved_seller_data(build_preparation(product()), r)


def test_excluded_logistics_and_unmatched_sku_rejected():
    r = record(); r['fields']['freight_template'] = '包邮'
    with pytest.raises(ValueError): merge_saved_seller_data(build_preparation(product()), r)
    r = record(); r['skus'] = [{'source_sku_id':'unknown', 'stock':0}]
    with pytest.raises(ValueError): merge_saved_seller_data(build_preparation(product()), r)
    assert read_capabilities()['seller_backend']['verified'] is False
