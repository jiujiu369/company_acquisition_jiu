"""Real legacy PowerShell parsing and cache invalidation for the folder button."""
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from app.main import create_app

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name!='nt',reason='Windows PowerShell is required')
def test_picker_script_parses_with_actual_windows_powershell():
    command = '''$taskTokens=$null; $taskErrors=$null;
    $taskAst=[System.Management.Automation.Language.Parser]::ParseFile("SCRIPT",[ref]$taskTokens,[ref]$taskErrors);
    $taskStrings=@($taskAst.FindAll({param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst]},$true)|ForEach-Object {$_.Value});
    [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);
    @{errors=@($taskErrors|ForEach-Object {$_.Message}); strings=$taskStrings}|ConvertTo-Json -Compress'''.replace('SCRIPT',str(ROOT/'scripts/select_folder.ps1'))
    r=subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,encoding='utf-8',timeout=20)
    assert r.returncode==0,r.stderr
    parsed=json.loads(r.stdout)
    assert not parsed['errors'],parsed['errors']
    assert '选择商品资料保存到的实际文件夹' in parsed['strings']


def test_page_assets_have_content_versions_and_respond_to_file_changes(tmp_path,monkeypatch):
    with TestClient(create_app(tmp_path/'data',start_worker=False)) as client:
        def versions():
            soup=BeautifulSoup(client.get('/').text,'html.parser')
            return {urlsplit(url).path:parse_qs(urlsplit(url).query).get('v') for url in
                [soup.select_one('link[rel=stylesheet]')['href'],*[node['src'] for node in soup.select('script[src]')]]}
        before=versions()
        assert set(before)=={'/static/css/collection.css','/static/js/preparation.js','/static/js/collection.js'}
        assert all(value and value[0] for value in before.values())
        # Only the static root is swapped, never production files.
        import app.main as main
        shadow=tmp_path/'assets';(shadow/'static/js').mkdir(parents=True);(shadow/'static/css').mkdir()
        for path in before:
            (shadow/path.lstrip('/')).write_bytes((ROOT/path.lstrip('/')).read_bytes())
        monkeypatch.setattr(main,'ROOT',shadow)
        assert versions()==before
        changed=shadow/'static/js/collection.js';changed.write_bytes(changed.read_bytes()+b'\n// new build\n')
        after=versions()
        assert after['/static/js/collection.js']!=before['/static/js/collection.js']
        assert after['/static/js/preparation.js']==before['/static/js/preparation.js']
