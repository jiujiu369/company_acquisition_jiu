"""Portable, append-only product packages; completion is proven by file hashes."""
import copy
import csv
import hashlib
import html
import io
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from app.preparation import FIELD_LABELS, FOCUSED_ATTRIBUTES
from app.export_data import build_export, export_provenance, product_csv_row, sku_csv_row, SKU_COLUMNS, PACKAGE_SKU_COLUMNS
from app.excel_export import excel_bytes
from app.excel_images import main_image_bytes

REQUIRED_FILES = {'product.json', 'product.csv', 'skus.csv', 'README.html', 'detail.html',
                  'media_manifest.json', 'sources.json', 'issues.json'}


def safe_name(value, limit=48):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:limit].rstrip(' .') or '商品'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '_' + value
    return value


def excel_filename(product, document, limit=120):
    title = (document.get('effective', {}).get('fields', {}).get('title') or {}).get('value') or product.title
    return safe_name(title, limit) + '.xlsx'


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def _cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value


def csv_bytes(columns, rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in columns})
    return stream.getvalue().encode('utf-8-sig')


def _csv(path, columns, rows):
    path.write_bytes(csv_bytes(columns, rows))


def _display(field):
    labels = {'not_set': '明确未设置', 'not_applicable': '不适用', 'not_obtained': '未获取', 'failed': '获取失败'}
    text = json.dumps(field.get('value'), ensure_ascii=False) if isinstance(field.get('value'), (dict, list)) else str(field.get('value', ''))
    return text if field['status'] == 'obtained' else labels.get(field['status'], '未知')


def _page(title, body):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:auto;padding:24px;overflow-wrap:anywhere}img{max-width:100%;height:auto}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px;text-align:left}figure{margin:20px 0}.warn{color:#92400e}pre{white-space:pre-wrap}a{color:#2456bb}</style><body>' + body + '</body></html>'


def write_package(data_dir: Path, product, document: dict, target: Path, check=None) -> dict:
    check = check or (lambda: None)
    root = Path(target).resolve()
    # Allow room for source ID, staging version, UUID asset filename and separators.
    available = min(48, 250 - len(str(root)) - 112)
    if available < 5:
        return {'status': 'FAILED', 'path': str(root), 'errors': ['目标路径过长，请选择较短的保存目录']}
    parent = root / (safe_name(product.source_product_id, 25) + '_' + safe_name(product.title, available))
    version = '版本_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8]
    path = parent / ('.pending_' + version)
    final = parent / version
    errors = []
    try:
        check()
        path.mkdir(parents=True, exist_ok=False)
        doc = copy.deepcopy(document)
        main_image = main_image_bytes(data_dir, product, doc)
        if doc.get('include_images') is False:
            doc['media'] = []
            doc['detail_blocks'] = []
            for sku in doc['effective']['skus']:
                sku['image_source_url'] = None
                sku['image_asset_id'] = None
                sku['image_relative_path'] = None
        by_id = {a.id: a for a in product.assets if a.original_asset_id is None}
        copied = {}
        wanted = {m['asset_id'] for m in doc['media'] if m.get('asset_id')}
        wanted.update(b['asset_id'] for b in doc['detail_blocks'] if b.get('asset_id'))
        wanted.update(s['image_asset_id'] for s in doc['effective']['skus'] if s.get('image_asset_id'))
        for aid in wanted:
            check()
            asset = by_id.get(aid)
            if not asset:
                raise OSError('缺少关联素材：' + aid)
            source = (Path(data_dir) / asset.relative_path).resolve()
            if not source.is_relative_to(Path(data_dir).resolve()) or not source.is_file():
                raise OSError('原始素材不存在或路径无效：' + aid)
            relative = 'assets/' + aid + source.suffix
            destination = path / relative
            destination.parent.mkdir(exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with source.open('rb') as src, destination.open('xb') as dst:
                while chunk := src.read(1024 * 1024):
                    check(); digest.update(chunk); size += len(chunk); dst.write(chunk)
            if digest.hexdigest() != asset.sha256 or size != asset.byte_size:
                raise OSError('原图完整性校验失败：' + aid)
            copied[aid] = relative
        # Derive paths from copied bytes, rather than trusting an old snapshot's suffix.
        for media in doc['media']:
            media['relative_path'] = copied.get(media.get('asset_id'))
            if not media['relative_path']:
                errors.append('素材未取得：' + media['role'])
        for block in doc['detail_blocks']:
            if block['type'] == 'image':
                block['relative_path'] = copied.get(block.get('asset_id'))
        for sku in doc['effective']['skus']:
            sku['image_relative_path'] = copied.get(sku.get('image_asset_id'))
            if sku.get('image_source_url') and not sku['image_relative_path']:
                errors.append('SKU 图片未取得：' + str(sku['order']))
        exported = build_export(doc, copied)
        workbook_name = None
        if doc.get('collection_profile') == 'sku_package':
            filename_limit = min(120, 250 - len(str(path)) - 6)
            if filename_limit < 5:
                raise OSError('目标路径过长，请选择较短的保存目录')
            workbook_name = excel_filename(product, doc, filename_limit)
            images = {doc['source_product_id']: main_image} if main_image else None
            (path / workbook_name).write_bytes(excel_bytes([doc], main_images=images))
        _json(path / 'product.json', exported)
        _json(path / 'provenance.json', export_provenance(doc))
        _json(path / 'media_manifest.json', doc['media'])
        _json(path / 'sources.json', doc['sources'])
        _json(path / 'issues.json', {'completeness': doc['completeness'], 'issues': doc['issues']})
        product_row = product_csv_row(exported)
        _csv(path / 'product.csv', list(product_row), [product_row])
        focused = doc.get('collection_profile') == 'sku_package'
        columns = list(PACKAGE_SKU_COLUMNS.values()) if focused else ['规格', *SKU_COLUMNS.values()]
        _csv(path / 'skus.csv', [*columns, '待确认字段', '状态说明'],
             [sku_csv_row(sku) for sku in exported['skus']])
        detail = ''
        for block in doc['detail_blocks']:
            if block['type'] == 'text':
                detail += '<p>' + html.escape(block.get('text', '')) + '</p>'
            elif block.get('relative_path'):
                detail += '<figure><img src="' + html.escape(block['relative_path'], quote=True) + '" alt="详情原图"></figure>'
            else:
                detail += '<p class="warn">此位置详情图未取得</p>'
                errors.append('详情图未取得')
        (path / 'detail.html').write_text(_page(product.title + ' · 详情', detail), encoding='utf-8')
        text = '<h1>' + html.escape(str(doc['effective']['fields']['title']['value'] or product.title)) + '</h1>'
        if focused:
            text += '<p><a href="' + quote(workbook_name) + '">下载商品资料 Excel 表格</a></p>'
        text += '<p>来源商品 ID：' + html.escape(product.source_product_id) + ' · 资料时间：' + html.escape(doc['captured_at']) + '</p>'
        text += ('<p>product.csv 保存商品名称和型号，skus.csv 每个 SKU 一行，保存 SKU 名称、包装数量、包装单位、每包价格及填写库存。每包价格不换算成单件价格；库存默认9999，属于填写配置。未取得的值留空。</p>' if focused else '<p>product.json 为整理后的最终资料，product.csv 每件商品一行，skus.csv 每个规格一行。展示价格、展示库存来自公开页面；记录价格、记录库存来自补充资料，待确认字段另列。未知值留空。</p>')
        text += '<p>请保留整个资料包及 assets 文件夹。<a href="detail.html">离线查看宝贝详情</a></p>'
        text += '<h2>商品资料</h2><table><tr><th>字段</th><th>最终值及状态</th><th>说明</th></tr>'
        for key, f in doc['effective']['fields'].items():
            if focused and key not in {'title', 'price', 'stock'}:
                continue
            if f['status'] != 'obtained':
                continue
            text += '<tr><td>' + html.escape(FIELD_LABELS[key]) + '</td><td>' + html.escape(_display(f)) + '</td><td>' + html.escape(str(f.get('reason') or '')) + (' · 人工修改' if 'fields.' + key in doc['manual_overrides'] else '') + (' · 待确认' if f.get('needs_confirmation') else '') + '</td></tr>'
        for a in doc['effective']['attributes']:
            if focused and a['source_name'] not in FOCUSED_ATTRIBUTES:
                continue
            text += '<tr><td>' + html.escape(a['source_name']) + '</td><td>' + html.escape(_display(a['field'])) + '</td><td>' + html.escape(str(a['field'].get('unit') or '')) + (' · 待确认' if a['field'].get('needs_confirmation') else '') + '</td></tr>'
        text += '</table><h2>SKU 规格及图片</h2>'
        for s in doc['effective']['skus']:
            if focused:
                text += '<h3>' + html.escape(_display(s['name'])) + '</h3>'
                text += '<p>包装数量：' + html.escape(_display(s['package_quantity'])) + ' ' + html.escape(_display(s['package_unit']))
                text += ' · 每包价格：' + html.escape(_display(s['package_price'])) + ' · 填写库存：' + html.escape(_display(s['stock'])) + '</p>'
                if s.get('image_relative_path'):
                    text += '<img src="' + html.escape(s['image_relative_path'], quote=True) + '" alt="SKU 原图">'
                continue
            text += '<h3>' + html.escape(' / '.join(str(v) for v in s['combination'].values()) or str(s['order'])) + '</h3>'
            display = s.get('buyer_display') or {}
            raw_price = display.get('price_raw') or {}
            price_type = raw_price.get('priceTitle') if isinstance(raw_price, dict) else None
            text += '<p>来源 SKU ID：' + html.escape(str(s['source_sku_id'] or '未获取'))
            text += ' · 展示价格：' + html.escape(str(display.get('price') if display.get('price') is not None else '未获取'))
            text += ('（' + html.escape(str(price_type)) + '）') if price_type else ''
            text += ' · 展示库存：' + html.escape(str(display.get('stock') if display.get('stock') is not None else '未获取'))
            text += ' · 记录价格：' + html.escape(_display(s['price'])) + ' · 记录库存：' + html.escape(_display(s['stock'])) + '</p>'
            pending = [name for name, value in s.items() if isinstance(value, dict) and value.get('needs_confirmation')]
            if pending:
                text += '<p class="warn">待确认字段：' + html.escape('、'.join(pending)) + '</p>'
            if s.get('image_relative_path'):
                text += '<img src="' + html.escape(s['image_relative_path'], quote=True) + '" alt="SKU 原图">'
        text += '<h2>素材用途与顺序</h2>'
        for m in doc['media']:
            text += '<p>' + html.escape(m['role']) + ' · ' + str(m['order']) + (' · 首图' if m['is_first'] else '') + '</p>'
            if m.get('relative_path'):
                tag = 'video controls' if m['role'] in {'product_video', 'after_purchase_video'} else 'img alt="商品原图"'
                text += '<' + tag + ' src="' + html.escape(m['relative_path'], quote=True) + '">' + ('</video>' if tag.startswith('video') else '')
            else:
                errors.append('素材未取得：' + m['role'])
        text += '<h2>缺失与待确认</h2><p>未取得商品字段：' + html.escape('、'.join(
            FIELD_LABELS.get(k, k) for k in exported['collection_summary']['missing_fields'])) + '</p>'
        for key, orders in exported['collection_summary']['unavailable_sku_fields'].items():
            text += '<p>SKU ' + html.escape('、'.join(map(str, orders))) + '：' + html.escape(FIELD_LABELS.get(key, key)) + '未取得。</p>'
        text += '<p>来源、人工修订及详细待确认说明见 <a href="sources.json">sources.json</a>、<a href="provenance.json">provenance.json</a>、<a href="issues.json">issues.json</a>。</p>'
        if product_row.get('状态说明'):
            text += '<p>' + html.escape(product_row['状态说明']) + '</p>'
        (path / 'README.html').write_text(_page(product.title, text), encoding='utf-8')
        check()
        if errors:
            _json(path / 'save.errors.json', errors)
            return {'status': 'INCOMPLETE', 'path': str(path), 'errors': errors}
        hashes = {}
        for f in path.rglob('*'):
            if f.is_file():
                with f.open('rb') as stream:
                    hashes[f.relative_to(path).as_posix()] = hashlib.file_digest(stream, 'sha256').hexdigest()
        _json(path / 'package.complete.json', {'schema_version': '2.0', 'files': hashes,
                                             'capabilities': ['excel'] if focused else [],
                                             'excel_file': workbook_name})
        os.rename(path, final)
        return verify_package(final)
    except OSError as exc:
        return {'status': 'FAILED', 'path': str(path), 'errors': [str(exc)]}


def verify_package(path: Path) -> dict:
    path = Path(path).resolve()
    errors = []
    try:
        manifest = json.loads((path / 'package.complete.json').read_text(encoding='utf-8'))
        if not REQUIRED_FILES.issubset(manifest['files']):
            raise ValueError('缺少必需资料文件')
        if manifest.get('schema_version') == '2.0' and 'provenance.json' not in manifest['files']:
            raise ValueError('缺少来源和修订记录')
        for relative, expected in manifest['files'].items():
            f = (path / relative).resolve()
            if not f.is_relative_to(path) or not f.is_file():
                errors.append('文件缺失或路径无效：' + relative)
                continue
            with f.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    errors.append('文件校验失败：' + relative)
        data = json.loads((path / 'product.json').read_text(encoding='utf-8'))
        if data.get('schema_version') == '2.0':
            if 'excel' in manifest.get('capabilities', []):
                workbook_name = manifest.get('excel_file') or '商品资料.xlsx'
                if (workbook_name not in manifest['files'] or Path(workbook_name).name != workbook_name
                        or not workbook_name.endswith('.xlsx')):
                    errors.append('缺少Excel商品资料表')
            if data.get('export_mode') != 'package' or any(not m.get('file_included') for m in data['media']):
                errors.append('完成的资料包必须包含声明的素材')
            refs = [m.get('relative_path') for m in data['media']]
            refs += [b.get('relative_path') for b in data['detail_blocks'] if b['type'] == 'image']
            refs += [s.get('image_relative_path') for s in data['skus']
                     if s.get('image_source_url') or 'image_relative_path' in s]
            if any(not ref or ref not in manifest['files'] or not (path / ref).resolve().is_relative_to(path)
                   or not (path / ref).is_file() for ref in refs):
                errors.append('资料引用的素材不存在、未校验或路径无效')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    return {'status': 'FAILED' if errors else 'SAVED', 'path': str(path), 'errors': errors,
            'verified_at': datetime.now(timezone.utc).isoformat()}
