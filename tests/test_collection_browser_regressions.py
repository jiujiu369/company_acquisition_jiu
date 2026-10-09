import json
from app.taobao.parser import TaobaoParser
from app.taobao.session import TaobaoSession


SHOP = 'https://bolisidg.tmall.com/category.htm'
CATALOG_API = 'https://h5api.m.tmall.com/h5/mtop.taobao.shop.simple.item.fetch/1.0/'


def test_modern_catalog_data_array_excludes_recommendations():
    payload = {'request_url': CATALOG_API, 'data': {
        'data': {'data': [{'itemId': '123', 'itemUrl': '//detail.tmall.com/item.htm?id=123'},
                          {'itemId': '456'}],
                 'recommendItems': [{'itemId': '999'}]}, 'ret': ['SUCCESS']}}
    assert TaobaoParser().discover_products('<div class="cardContainer--test">商品</div>', SHOP, [payload]) == [
        'https://detail.tmall.com/item.htm?id=123', 'https://detail.tmall.com/item.htm?id=456']


def test_modern_catalog_captures_jsonp_even_after_unrelated_responses(tmp_path):
    class Response:
        headers = {'content-type': 'application/javascript'}
        url = CATALOG_API + '?callback=mtopjsonp1'

        def text(self):
            return 'mtopjsonp1(' + json.dumps({'data': {'data': [{'itemId': '123'}]}}) + ');'

    session = TaobaoSession(tmp_path)
    session.network_data = [{'request_url': 'https://example.test/analytics', 'data': {}} for _ in range(30)]
    session._capture_response(Response())
    assert TaobaoParser().discover_products('<div>商品</div>', SHOP, session.network_data) == [
        'https://detail.tmall.com/item.htm?id=123']


def test_first_screen_item_info_is_catalog_but_other_data_is_not():
    payload = {'request_url': 'https://h5api.m.tmall.com/h5/mtop.taobao.shop.simple.fetch/1.0/',
               'data': {'data': {'itemInfoDTO': {'data': [{'itemId': '123'}]},
                                 'userInfo': {'itemId': '999'},
                                 'recommendItems': [{'itemId': '888'}]}}}
    assert TaobaoParser().discover_products('<div>商品</div>', SHOP, [payload]) == [
        'https://detail.tmall.com/item.htm?id=123']


def test_unknown_api_data_array_is_not_treated_as_shop_catalog():
    payload = {'request_url': 'https://example.test/ads',
               'data': {'data': [{'itemId': '999'}]}}
    assert TaobaoParser().discover_products('<div>商品</div>', SHOP, [payload]) == []


def test_browser_discovers_script_rendered_cards_without_anchor_links(tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel='chrome')
        context = browser.new_context()

        def route(request):
            if 'simple.item.fetch' in request.request.url:
                request.fulfill(content_type='application/javascript', body=
                    'mtopjsonp1({"data":{"data":[{"itemId":"123"},{"itemId":"456"}]}});')
            else:
                request.fulfill(content_type='text/html', body=f'''
                    <h1>Shop</h1><script>function mtopjsonp1(payload) {{
                        document.body.insertAdjacentHTML('beforeend',
                            payload.data.data.map(item => '<div class="cardContainer--test">Product</div>').join(''));
                    }}</script><script src="{CATALOG_API}?callback=mtopjsonp1"></script>''')

        context.route('**/*', route)
        session = TaobaoSession(tmp_path)
        session.context, session.page = context, context.new_page()
        session._attach_page_handlers()
        try:
            collector_urls = []
            for html, url, network in session.iter_shop_pages('http://fixture.test/category.htm'):
                collector_urls.extend(TaobaoParser().discover_products(html, SHOP, network))
            assert list(dict.fromkeys(collector_urls)) == [
                'https://detail.tmall.com/item.htm?id=123', 'https://detail.tmall.com/item.htm?id=456']
        finally:
            context.close()
            browser.close()


def test_browser_login_resume_uses_same_context_and_revisits_target(tmp_path):
    import pytest
    from playwright.sync_api import sync_playwright
    from app.taobao.session import ManualRequired
    target = 'https://item.taobao.com/item.htm?id=123'
    visited = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel='chrome')
        context = browser.new_context()
        page = context.new_page()
        def route(r):
            visited.append(r.request.url)
            if 'login.taobao.com' in r.request.url:
                r.fulfill(content_type='text/html; charset=utf-8', body='<input id="fm-login-id">')
            elif 'fixture-login=yes' in r.request.headers.get('cookie',''):
                r.fulfill(content_type='text/html; charset=utf-8', body='<h1>已登录商品</h1><div id="J_DivItemDesc"><img data-src="https://img.alicdn.com/detail.png"></div>')
            else:
                r.fulfill(status=302, headers={'location':'https://login.taobao.com/member/login.jhtml'})
        context.route('**/*', route)
        session = TaobaoSession(tmp_path)
        session.context, session.page = context, page
        session._attach_page_handlers()
        try:
            with pytest.raises(ManualRequired, match='登录'):
                session.open(target)
            assert session.manual_pending
            context.add_cookies([{'name':'fixture-login','value':'yes','domain':'.taobao.com','path':'/'}])
            html, final, _ = session.open(target)
            assert final == target and '已登录商品' in html
            assert session.page is page and session.context is context
            assert visited.count(target) == 2
            context.clear_cookies()
            with pytest.raises(ManualRequired):
                session.open(target)
            assert visited.count(target) == 3
        finally:
            context.close()
            browser.close()
