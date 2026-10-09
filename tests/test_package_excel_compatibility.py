import json
from pathlib import Path

from app.preparation import apply_overrides, use_package_profile
from app.product_package import verify_package, write_package
from test_product_package import sample


def focused_package(tmp_path):
    product, document, _ = sample(tmp_path)
    result = write_package(tmp_path, product, apply_overrides(use_package_profile(document), {}), tmp_path / 'saved')
    assert result['status'] == 'SAVED'
    return Path(result['path'])


def remove_excel(folder, *, historical):
    manifest_path = folder / 'package.complete.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if historical:
        manifest.pop('capabilities', None)
    filename = next(name for name in manifest['files'] if name.endswith('.xlsx'))
    manifest['files'].pop(filename)
    (folder / filename).unlink()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')


def test_historical_focused_package_without_excel_still_verifies(tmp_path):
    folder = focused_package(tmp_path)
    remove_excel(folder, historical=True)
    assert verify_package(folder)['status'] == 'SAVED'


def test_new_package_declares_and_requires_excel(tmp_path):
    folder = focused_package(tmp_path)
    manifest = json.loads((folder / 'package.complete.json').read_text(encoding='utf-8'))
    assert 'excel' in manifest['capabilities']
    remove_excel(folder, historical=False)
    assert verify_package(folder)['status'] == 'FAILED'


def test_historical_fixed_excel_filename_still_verifies(tmp_path):
    folder = focused_package(tmp_path)
    manifest_path = folder / 'package.complete.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    filename = manifest.pop('excel_file')
    (folder / filename).rename(folder / '商品资料.xlsx')
    manifest['files']['商品资料.xlsx'] = manifest['files'].pop(filename)
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    assert verify_package(folder)['status'] == 'SAVED'
