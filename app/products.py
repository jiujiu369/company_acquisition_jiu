"""Collection storage with compatible legacy columns and immutable captures."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint, event, inspect
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.tasks import utc_now


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("source_platform", "source_product_id", name="uq_product_source"),)

    local_product_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_platform: Mapped[str] = mapped_column(String(20), nullable=False, default="taobao")
    source_product_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    current_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(14, 2))
    category: Mapped[str | None] = mapped_column(String(255))
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    model: Mapped[str | None] = mapped_column(String(255))
    specification: Mapped[str | None] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    # Existing databases require these columns when a collection saves a product.
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="COLLECTED")
    asset_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")

    skus: Mapped[list["ProductSku"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    assets: Mapped[list["ProductAsset"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    captures: Mapped[list["ProductCapture"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class ProductSku(Base):
    __tablename__ = "product_skus"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.local_product_id"), nullable=False, index=True)
    source_sku_id: Mapped[str | None] = mapped_column(String(100))
    properties: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    price: Mapped[float | None] = mapped_column(Numeric(14, 2))
    stock: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(Text)
    product: Mapped[Product] = relationship(back_populates="skus")


class ProductAsset(Base):
    __tablename__ = "product_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.local_product_id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)  # Keep legacy video rows distinguishable from originals.
    role: Mapped[str] = mapped_column(String(24), nullable=False)  # main / detail / sku
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_asset_id: Mapped[str | None] = mapped_column(ForeignKey("product_assets.id"))  # Exclude historical derivatives.
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    product: Mapped[Product] = relationship(back_populates="assets")


class ProductCapture(Base):
    __tablename__ = "product_captures"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.local_product_id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36))
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    product: Mapped[Product] = relationship(back_populates="captures")


class CollectionJob(Base):
    __tablename__ = "collection_jobs"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # product / shop
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    preset: Mapped[dict] = mapped_column(JSON, nullable=False)  # Required by existing collection_jobs tables.
    result_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    manual_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


class PreparationCapture(Base):
    __tablename__ = 'preparation_captures'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(ForeignKey('products.local_product_id'), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(24), default='source')
    document: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ManualRevision(Base):
    __tablename__ = 'preparation_manual_revisions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id: Mapped[str] = mapped_column(ForeignKey('products.local_product_id'), index=True)
    overrides: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class PreparationBatch(Base):
    __tablename__ = 'preparation_batches'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_path: Mapped[str] = mapped_column(Text)
    target_path: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class PackageSave(Base):
    __tablename__ = 'preparation_package_saves'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey('tasks.task_id'), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey('products.local_product_id'), index=True)
    capture_id: Mapped[str | None] = mapped_column(String(36))
    revision_id: Mapped[str | None] = mapped_column(String(36))
    target_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class PreparationSetting(Base):
    __tablename__ = 'preparation_settings'
    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


@event.listens_for(PreparationCapture, 'before_update')
@event.listens_for(ManualRevision, 'before_update')
def protect_preparation_history(_mapper, _connection, _target):
    raise ValueError('准备快照和人工修订不可覆盖')


@event.listens_for(Product, "before_update")
def protect_raw_data(_mapper, _connection, target: Product):
    history = inspect(target).attrs.raw_data.history
    if history.has_changes():
        raise ValueError("raw_data 是不可覆盖的原始采集记录")


@event.listens_for(ProductCapture, "before_update")
def protect_capture(_mapper, _connection, _target: ProductCapture):
    raise ValueError("原始采集快照不可修改")


@event.listens_for(ProductAsset, "before_update")
def protect_asset(_mapper, _connection, target: ProductAsset):
    state = inspect(target)
    if target.original_asset_id is None and any(
        state.attrs[name].history.has_changes()
        for name in ("relative_path", "sha256", "byte_size", "source_url")
    ):
        raise ValueError("original 素材不可覆盖")


