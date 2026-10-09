from app.taobao.parser import TaobaoParser

URL = 'https://item.taobao.com/item.htm?id=123'


def test_zero_and_price_range_are_not_lost():
    p = TaobaoParser().parse_product('<h1>商品</h1><script>window.__INITIAL_STATE__={"skus":[{"skuId":"1","quantity":0,"price":"10-20"}]};</script>', URL)
    assert p.skus[0]['stock'] == 0
    assert p.skus[0]['price'] is None
    assert p.skus[0]['price_raw'] == '10-20'


def test_shared_image_roles_and_description_repetitions():
    p = TaobaoParser().parse_product('<h1>商品</h1><meta property="og:image" content="https://img.alicdn.com/a.jpg"><div id="J_DivItemDesc"><p>前</p><img src="https://img.alicdn.com/a.jpg"><p>后</p><img src="https://img.alicdn.com/a.jpg"></div><div class="recommend">广告</div>', URL)
    assert {i['role'] for i in p.images} == {'main', 'detail'}
    assert [(b['type'], b.get('text')) for b in p.detail_blocks] == [('text', '前'), ('image', None), ('text', '后'), ('image', None)]


def test_response_for_another_product_is_ignored():
    p = TaobaoParser().parse_product('<h1>本商品</h1>', URL, [{'itemId': '456', 'skus': [{'skuId': 'other', 'price': '8'}]}])
    assert p.skus == []


def test_picture_source_preserves_position_and_normalizes_sku_image():
    p=TaobaoParser().parse_product('<h1>商品</h1><div id="J_DivItemDesc">前<picture><source srcset="https://img.alicdn.com/a.jpg 1x"></picture>后</div><script>window.__INITIAL_STATE__={"skus":[{"skuId":"A","imageUrl":"//img.alicdn.com/a.jpg"}]};</script>',URL)
    assert [b['type'] for b in p.detail_blocks]==['text','image','text']
    assert p.skus[0]['image_url']=='https://img.alicdn.com/a.jpg'


def test_optional_material_requires_product_scope():
    p=TaobaoParser().parse_product('<h1>商品</h1><div class="tb-gallery"><video src="https://cloud.video.taobao.com/a.mp4"></video></div><div class="recommend"><video src="https://cloud.video.taobao.com/ad.mp4"></video></div>',URL)
    assert {'url':'https://cloud.video.taobao.com/a.mp4','role':'product_video'} in p.images
    assert all('ad.mp4' not in m['url'] for m in p.images)


def test_all_lazy_detail_resource_attributes_keep_text_order():
    for attribute in ('data-lazyload','data-srcset','srcset'):
        p=TaobaoParser().parse_product(f'<h1>商品</h1><div id="J_DivItemDesc">前<div {attribute}="https://img.alicdn.com/a.jpg"></div>后</div>',URL)
        assert [b['type'] for b in p.detail_blocks]==['text','image','text']
