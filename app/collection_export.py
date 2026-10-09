"""Atomic JSON/CSV metadata and byte-exact original image archives."""

import csv
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from app.product_service import source_shop
from app.taobao.parser import ParsedProduct

CSV_FIELDS = ["product_id","source_product_id","source_url","title","shop","price","category",
              "attributes","skus","model","specification","collected_at"]


def _asset_record(data_dir, asset):
    path = Path(data_dir)/asset.relative_path
    return {"asset_id":asset.id,"source_url":asset.source_url,"local_path":str(path),
            "mime":asset.mime_type,"format":path.suffix.lstrip("."),"width":asset.width,"height":asset.height,
            "byte_size":asset.byte_size,"sha256":asset.sha256,"downloaded_at":asset.created_at.isoformat()+"Z",
            "role":asset.role}


def write_product_manifest(data_dir, product, parsed, errors, aliases=None):
    folder = Path(data_dir)/"products"/product.local_product_id/"source"
    folder.mkdir(parents=True,exist_ok=True)
    by_url = {a.source_url:a for a in product.assets if a.original_asset_id is None and a.kind == "image"}
    by_url.update(aliases or {})
    failures = {e["url"]:e["error"] for e in errors}
    images = []
    for index,item in enumerate(parsed.images):
        url, role = item["url"],item.get("role","detail")
        asset = by_url.get(url)
        record = _asset_record(data_dir,asset) if asset else {"source_url":url,"asset_id":None,"error":failures.get(url,"未下载")}
        images.append({**record,"role":role,"order":index+1})
    manifest = {"product_id":product.local_product_id,"source_platform":product.source_platform,
        "source_product_id":product.source_product_id,"source_url":product.source_url,
        "title":parsed.title,"shop":source_shop(parsed.raw_data),"price":parsed.price,"category":parsed.category,
        "attributes":parsed.attributes,"skus":parsed.skus,"model":parsed.model,"specification":parsed.specification,
        "images":images,"videos":[],"collected_at":product.collected_at.isoformat()+"Z"}
    target = folder/"product.json"
    temporary = folder/"product.json.tmp"
    temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    os.replace(temporary,target)
    csv_temp = folder/"product.csv.tmp"
    with csv_temp.open("w",encoding="utf-8-sig",newline="") as stream:
        writer = csv.DictWriter(stream,fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerow({key: json.dumps(manifest[key],ensure_ascii=False) if isinstance(manifest.get(key),(list,dict)) else manifest.get(key) for key in CSV_FIELDS})
    os.replace(csv_temp,folder/"product.csv")
    return target


def ensure_manifest(data_dir, product):
    folder = Path(data_dir)/"products"/product.local_product_id/"source"
    if (folder/"product.json").is_file() and (folder/"product.csv").is_file():
        return folder
    current = product.current_data or {}
    parsed = ParsedProduct(source_product_id=product.source_product_id,source_url=product.source_url,
        title=product.title,price=str(product.price) if product.price is not None else None,category=product.category,
        attributes=product.attributes,skus=current.get("skus",[]),model=product.model,specification=product.specification,
        raw_data=product.raw_data, images=[{"url":a.source_url,"role":a.role} for a in product.assets
                                         if a.original_asset_id is None and a.kind == "image"])
    write_product_manifest(data_dir,product,parsed,[])
    return folder


def collection_zip(data_dir, products, documents=None):
    from app.preparation_service import get_document
    from app.product_package import write_package
    from sqlalchemy.orm import object_session
    output = tempfile.SpooledTemporaryFile(max_size=8*1024*1024)
    try:
        with zipfile.ZipFile(output,"w",compression=zipfile.ZIP_DEFLATED) as archive:
            for product in products:
                session = object_session(product)
                document = (documents or {}).get(product.local_product_id) or get_document(session, product)
                package = write_package(Path(data_dir), product, document, Path(data_dir)/'exports'/'packages')
                folder = Path(package['path'])
                # Even a partial package contains the acquired values and explicit missing-media records.
                if not folder.is_dir() or not (folder/'product.json').is_file():
                    raise ValueError('资料包无法生成：' + '; '.join(package.get('errors', [])))
                prefix = product.local_product_id+"/" if len(products)>1 else ""
                for path in folder.rglob('*'):
                    if path.is_file():
                        archive.write(path, prefix+path.relative_to(folder).as_posix())
        output.seek(0)
        return output
    except Exception:
        output.close()
        raise
