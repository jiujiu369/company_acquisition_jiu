"""Read a captured main image for an offline Excel preview; never fetch URLs."""
import hashlib
import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


def main_image_bytes(data_dir, product, document):
    assets = {a.id: a for a in product.assets if a.original_asset_id is None and a.kind == 'image'}
    if 'excel_main_image' in document:
        reference = document['excel_main_image']
        candidates = [assets.get(reference.get('asset_id'))] if reference else []
    else:
        media = [m for m in document.get('media', []) if m.get('role') == 'main']
        candidates = [assets.get(m.get('asset_id')) for m in sorted(media, key=lambda m: m.get('order', 0))]
        # No captured reference means unknown, even if a later task collected
        # an unrelated main image for the same product.
    root = Path(data_dir).resolve()
    for asset in candidates:
        if not asset:
            continue
        try:
            path = (root / asset.relative_path).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != asset.byte_size:
                continue
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != asset.sha256:
                continue
            with Image.open(io.BytesIO(content)) as image:
                preview = ImageOps.exif_transpose(image).convert('RGB')
                preview.thumbnail((320, 320))
                output = io.BytesIO()
                preview.save(output, format='PNG')
                return output.getvalue()
        except (OSError, ValueError, UnidentifiedImageError):
            continue
    return None
