"""Exercise the real native picker from the page against an isolated database."""
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright
from app.main import create_app
from app.preparation_service import setting


def main():
    with tempfile.TemporaryDirectory(prefix='native-picker-') as workspace:
        chosen = Path(workspace) / '中文 资料文件夹'
        chosen.mkdir()
        app = create_app(Path(workspace) / 'data', start_worker=False)
        sock = socket.socket(); sock.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started: break
                time.sleep(.05)
            assert server.started
            with app.state.session_factory.begin() as session:
                setting(session, 'last_directory', {'path':str(chosen)})
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel='chrome')
                page = browser.new_page()
                errors=[]; page.on('pageerror', lambda error:errors.append(str(error)))
                page.goto('http://127.0.0.1:'+str(sock.getsockname()[1]), wait_until='networkidle')
                assert page.locator('#save-directory').input_value() == str(chosen)
                assert page.locator('#save-directory').get_attribute('readonly') is not None
                page.locator('#choose-directory').click()
                print('Real native dialog opened; confirm current test folder.', flush=True)
                page.wait_for_function("document.querySelector('#directory-status').textContent.startsWith('已选择：')", timeout=180000)
                assert page.locator('#save-directory').input_value() == str(chosen)
                assert page.locator('#choose-directory').is_enabled()
                assert not errors, errors
                print(json.dumps({'native_button_to_path':'passed', 'chinese_and_spaces':'passed',
                    'readonly_location':'passed','feedback':page.locator('#directory-status').inner_text(),
                    'page_errors':errors}, ensure_ascii=False),flush=True)
                page.screenshot(path='artifacts/native-folder-selection-page.png',full_page=True)
                browser.close()
        finally:
            server.should_exit=True; thread.join(timeout=10); sock.close()


if __name__=='__main__': main()
