"""Append-only product captures and collection persistence."""

import copy
from decimal import Decimal
from sqlalchemy import select
from app.products import Product, ProductCapture, ProductSku, PreparationCapture
from app.tasks import utc_now


def source_shop(raw: dict) -> str | None:
    for key in ("shop_name", "shopName", "seller_name", "sellerName", "store_name", "storeName"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for entry in raw.get("structured_data", []):
        if not isinstance(entry, dict):
            continue
        result = (((entry.get("loaderData") or {}).get("home") or {}).get("data") or {}).get("res") or {}
        seller = result.get("seller") if isinstance(result, dict) else {}
        if isinstance(seller, dict):
            value = seller.get("shopName") or seller.get("sellerNick")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def save_collected(session, parsed, task_id: str) -> tuple[Product, bool]:
    current = {
        "title": parsed.title, "price": parsed.price, "category": parsed.category,
        "attributes": copy.deepcopy(parsed.attributes), "skus": copy.deepcopy(parsed.skus),
        "model": parsed.model, "specification": parsed.specification,
        "detail_blocks": copy.deepcopy(parsed.detail_blocks), "category_data": copy.deepcopy(parsed.category_data),
    }
    platform = "tmall" if "tmall.com" in parsed.source_url else "taobao"
    existing = session.scalar(select(Product).where(
        Product.source_platform == platform, Product.source_product_id == parsed.source_product_id
    ))
    if existing:
        # Old current_data may include undocumented manual values: preserve it before refresh.
        session.add(PreparationCapture(product_id=existing.local_product_id, task_id=task_id,
            kind='legacy', document={'legacy_current_data': copy.deepcopy(existing.current_data),
                                     'reason': '保留刷新前原值；不能推定哪些旧字段曾人工修改'}))
        session.add(ProductCapture(product_id=existing.local_product_id, task_id=task_id,
                                   raw_data=copy.deepcopy(parsed.raw_data)))
        existing.collected_at = utc_now()
        existing.updated_at = utc_now()
        existing.source_url = parsed.source_url
        existing.current_data = {**existing.current_data, **current}
        existing.title = parsed.title
        existing.price = Decimal(parsed.price) if parsed.price else None
        existing.category = parsed.category
        existing.attributes = copy.deepcopy(parsed.attributes)
        existing.model, existing.specification = parsed.model, parsed.specification
        _save_skus(session, existing, parsed.skus)
        return existing, True
    product = Product(source_platform=platform, source_product_id=parsed.source_product_id,
                      source_url=parsed.source_url, raw_data=copy.deepcopy(parsed.raw_data),
                      current_data=current, title=parsed.title,
                      price=Decimal(parsed.price) if parsed.price else None,
                      category=parsed.category, attributes=copy.deepcopy(parsed.attributes),
                      model=parsed.model, specification=parsed.specification,
                      review_status="COLLECTED")
    session.add(product)
    session.flush()
    session.add(ProductCapture(product_id=product.local_product_id, task_id=task_id,
                               raw_data=copy.deepcopy(parsed.raw_data)))
    _save_skus(session, product, parsed.skus)
    return product, False


def _save_skus(session, product, skus):
    # Keep historical SKU rows; immutable captures retain each source version.
    for sku in skus:
        row = next((old for old in product.skus if
            (sku.get("source_sku_id") and old.source_sku_id == sku["source_sku_id"]) or
            (not sku.get("source_sku_id") and not old.source_sku_id and old.properties == (sku.get("properties") or {}))), None)
        if row is None:
            row = ProductSku(product_id=product.local_product_id, source_sku_id=sku.get("source_sku_id"))
            session.add(row)
        row.properties = copy.deepcopy(sku.get("properties") or {})
        row.price = Decimal(sku["price"]) if sku.get("price") else None
        row.stock = int(sku["stock"]) if sku.get("stock") is not None and str(sku["stock"]).isdigit() else None
        row.image_url = sku.get("image_url")


