from fastapi.testclient import TestClient
from app.main import create_app


def test_path_controls_and_settings_routes(tmp_path):
    with TestClient(create_app(tmp_path / 'data', start_worker=False)) as c:
        page = c.get('/').text
        assert 'id="save-directory"' in page and 'id="choose-directory"' in page
        assert 'id="collection-mode"' in page
        assert c.get('/api/collection/preparation/settings').status_code == 200
        assert c.post('/api/collection/preparation/directory/check', json={'path':str(tmp_path)}).json()['writable'] is True
        r = c.post('/api/collection/preparation/directory/check', json={'path':'relative'})
        assert r.status_code == 422


def test_picker_cancel_and_selected_path(tmp_path, monkeypatch):
    import app.preparation_routes as routes
    with TestClient(create_app(tmp_path / 'data', start_worker=False)) as c:
        monkeypatch.setattr(routes, 'pick_directory', lambda initial: None)
        assert c.post('/api/collection/preparation/directory/select', json={'path':''}).json()['canceled'] is True
        monkeypatch.setattr(routes, 'pick_directory', lambda initial: str(tmp_path))
        result = c.post('/api/collection/preparation/directory/select', json={'path':''}).json()
        assert result['path'] == str(tmp_path)
