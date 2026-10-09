"""Selection must use native browsing; write errors must not lose the chosen path."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_save_location_is_display_only(tmp_path):
    with TestClient(create_app(tmp_path / 'data', start_worker=False)) as client:
        page = BeautifulSoup(client.get('/').text, 'html.parser')
        location = page.select_one('#save-directory')
        assert location.has_attr('readonly')
        assert '输入' not in location.get('placeholder', '')


def test_unwritable_selection_is_shown_but_start_check_rejects(tmp_path, monkeypatch):
    import app.preparation_routes as routes
    import app.preparation_service as service
    chosen = tmp_path / '中文 新建资料夹'
    chosen.mkdir()
    monkeypatch.setattr(routes, 'pick_directory', lambda initial: str(chosen))
    with TestClient(create_app(tmp_path / 'data', start_worker=False)) as client:
        def denied(**kwargs):
            raise PermissionError('test directory cannot be written')
        monkeypatch.setattr(service.tempfile, 'NamedTemporaryFile', denied)
        response = client.post('/api/collection/preparation/directory/select', json={'path':''})
        assert response.status_code == 200
        assert response.json()['path'] == str(chosen)
        assert response.json()['writable'] is False
        assert '不可写' in response.json()['error']
        assert client.get('/api/collection/preparation/settings').json()['last_directory'] == str(chosen)
        assert client.post('/api/collection/preparation/directory/check', json={'path':str(chosen)}).status_code == 422


@pytest.mark.skipif(os.name != 'nt', reason='Windows shell is required')
def test_native_shell_dialog_contract_without_opening_ui():
    from app.preparation_routes import folder_picker_executable
    result = subprocess.run([str(folder_picker_executable()), '--check'],
        capture_output=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    checked = json.loads(result.stdout)
    assert checked['folder_mode'] is True
    assert checked['filesystem_only'] is True
