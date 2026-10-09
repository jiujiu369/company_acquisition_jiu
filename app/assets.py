"""Byte-exact immutable original image downloads."""

import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image

from app.products import ProductAsset


ALLOWED_MEDIA_HOSTS = ("alicdn.com", "taobaocdn.com", "tbcdn.cn", "taobao.com", "tmall.com", "aliyuncs.com")
IMAGE_FORMATS = {"image/jpeg": (".jpg", "JPEG"), "image/png": (".png", "PNG"),
                 "image/webp": (".webp", "WEBP"), "image/gif": (".gif", "GIF")}
MAX_IMAGE_BYTES = 40_000_000
VIDEO_FORMATS = {'video/mp4': ('.mp4', 'MP4')}
MEDIA_FORMATS = {**IMAGE_FORMATS, **VIDEO_FORMATS}


def _media_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_MEDIA_HOSTS):
        raise ValueError("只允许 HTTPS 淘宝/天猫素材 CDN URL")
    return url


class AssetStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.check = lambda: None

    def download_original(self, product_id: str, source_url: str, role: str,
                          cookies: dict[str, str] | None = None, referer: str | None = None) -> ProductAsset:
        url = _media_url(source_url)
        temp_dir = self.data_dir / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with httpx.Client(timeout=30, follow_redirects=False, cookies=cookies,
                              headers={"User-Agent": "Mozilla/5.0", "Referer": referer or "https://www.taobao.com/"}) as client:
                for _ in range(4):
                    self.check()
                    with client.stream("GET", url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = _media_url(urljoin(url, str(response.headers.get("location", ""))))
                            continue
                        response.raise_for_status()
                        mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                        if mime not in MEDIA_FORMATS or (mime in VIDEO_FORMATS and role not in {'product_video', 'after_purchase_video'}):
                            raise ValueError(f"不支持的素材类型：{mime or '未知'}")
                        limit = 200_000_000 if mime in VIDEO_FORMATS else MAX_IMAGE_BYTES
                        digest = hashlib.sha256()
                        size = 0
                        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as stream:
                            temp_path = Path(stream.name)
                            for chunk in response.iter_bytes(1024 * 256):
                                self.check()
                                size += len(chunk)
                                if size > limit:
                                    raise ValueError("素材超过下载大小限制")
                                digest.update(chunk)
                                stream.write(chunk)
                        return self._persist_original(product_id, source_url, role, mime,
                                                      temp_path, digest.hexdigest(), size)
                raise ValueError("素材重定向次数过多")
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()

    def save_original_bytes(self, product_id: str, source_url: str, role: str,
                            mime: str, content: bytes) -> ProductAsset:
        """Used for deterministic tests and already fetched bytes; original bytes stay exact."""
        if mime not in MEDIA_FORMATS or (mime in VIDEO_FORMATS and role not in {'product_video', 'after_purchase_video'}):
            raise ValueError("不支持的素材类型")
        temp_dir = self.data_dir / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False) as stream:
            stream.write(content)
            path = Path(stream.name)
        try:
            return self._persist_original(product_id, source_url, role, mime, path,
                                          hashlib.sha256(content).hexdigest(), len(content))
        finally:
            path.unlink(missing_ok=True)

    def _persist_original(self, product_id: str, source_url: str, role: str, mime: str,
                          temp_path: Path, digest: str, size: int) -> ProductAsset:
        ext = MEDIA_FORMATS[mime][0]
        asset_id = str(uuid.uuid4())
        relative = Path("products") / product_id / "original" / f"{asset_id}{ext}"
        destination = self.data_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(temp_path, destination)  # fails if target already exists; never replaces
        width = height = None
        try:
            if mime in VIDEO_FORMATS:
                with destination.open('rb') as stream:
                    if stream.read(12)[4:8] != b'ftyp':
                        raise ValueError('MP4 素材头无效，可能为验证页面')
            else:
                with Image.open(destination) as image:
                    image.verify()
                with Image.open(destination) as image:
                    width, height = image.size
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ProductAsset(id=asset_id, product_id=product_id, kind="video" if mime in VIDEO_FORMATS else "image", role=role,
                            source_url=source_url, relative_path=relative.as_posix(),
                            sha256=digest, mime_type=mime, width=width, height=height,
                            byte_size=size)

