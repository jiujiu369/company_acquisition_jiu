"""Verify the live local page's handler without choosing a user's real folder."""
import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    calls=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,channel='chrome')
        page=browser.new_page(viewport={'width':1280,'height':900})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.route('**/api/collection/preparation/settings',lambda route:route.fulfill(json={'last_directory':''}))
        page.on('request',lambda request:calls.append(request.method) if request.url.endswith('/directory/select') else None)
        def cancel(route):
            route.fulfill(json={'path':None,'canceled':True})
        if '--native' not in sys.argv:
            page.route('**/api/collection/preparation/directory/select',cancel)
        page.goto('http://127.0.0.1:8000/',wait_until='networkidle')
        page.locator('#choose-directory').click()
        if '--native' in sys.argv: print('Real production picker opened; cancel to finish verification.',flush=True)
        page.get_by_text('已取消选择，原路径保留。',exact=True).wait_for(timeout=180000)
        assert calls==['POST'] and not errors
        assert page.locator('#choose-directory').is_enabled()
        assert page.locator('#save-directory').get_attribute('readonly') is not None
        styles=page.locator('#save-directory').evaluate('e=>({height:e.getBoundingClientRect().height,row:getComputedStyle(e.parentElement).display})')
        assert styles['height']>=44 and styles['row']=='flex',styles
        evidence=Path('artifacts/folder-button-fixed.png').resolve()
        page.screenshot(path=str(evidence),full_page=True)
        print(json.dumps({'live_page':'passed','button_request':calls,'cancel_feedback':'passed',
            'new_styles':styles,'page_errors':errors,'screenshot':str(evidence)},ensure_ascii=False))
        browser.close()


if __name__=='__main__':main()
