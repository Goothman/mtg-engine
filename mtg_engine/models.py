"""Database models."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class CardPrinting(Base):
    """One printing of a card in a specific set (e.g. Lightning Bolt, Alpha)."""

    __tablename__ = "card_printings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # provider id (Scryfall uuid, ...)
    game: Mapped[str] = mapped_column(String(32), index=True)
    set_code: Mapped[str] = mapped_column(String(16), index=True)
    set_name: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(256), index=True)
    name_norm: Mapped[str] = mapped_column(String(256), index=True)
    collector_number: Mapped[str | None] = mapped_column(String(16))
    rarity: Mapped[str | None] = mapped_column(String(32))
    type_line: Mapped[str | None] = mapped_column(String(256))
    image_url: Mapped[str | None] = mapped_column(Text)
    cardmarket_id: Mapped[int | None] = mapped_column(Integer, index=True)
    is_target: Mapped[bool] = mapped_column(Boolean, default=True)
    price_eur: Mapped[float | None] = mapped_column(Float)
    price_low_eur: Mapped[float | None] = mapped_column(Float)
    price_source: Mapped[str | None] = mapped_column(String(64))
    price_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Listing(Base):
    __tablename__ = "listings"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # "<marketplace>:<external_id>"
    marketplace: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(96))
    game: Mapped[str] = mapped_column(String(32), index=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    price_eur: Mapped[float | None] = mapped_column(Float)
    city: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))
    postal_code: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(4), default="ES")
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    geo_precision: Mapped[str | None] = mapped_column(String(16))  # exact | city | province | none
    seller_id: Mapped[str | None] = mapped_column(String(96))
    seller_name: Mapped[str | None] = mapped_column(String(128))
    images: Mapped[list[Any]] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active | gone
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)

    # valuation (recomputed after every match pass)
    est_total_value: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    est_score: Mapped[float] = mapped_column(Float, default=0.0)
    value_ratio: Mapped[float | None] = mapped_column(Float)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    editions: Mapped[list[Any]] = mapped_column(JSON, default=list)  # detected target editions
    flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)

    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    matches: Mapped[list["Match"]] = relationship(back_populates="listing", cascade="all, delete-orphan")
    deal: Mapped["Deal | None"] = relationship(back_populates="listing", uselist=False)

    __table_args__ = (UniqueConstraint("marketplace", "external_id"),)


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    card_id: Mapped[str] = mapped_column(ForeignKey("card_printings.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))  # text | vision | text+vision | manual
    matched_text: Mapped[str | None] = mapped_column(Text)
    unit_value_eur: Mapped[float | None] = mapped_column(Float)
    edition_ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="auto")  # auto | confirmed | rejected

    listing: Mapped[Listing] = relationship(back_populates="matches")
    card: Mapped[CardPrinting] = relationship()


class Deal(Base):
    """Downstream workflow state for a promising listing. Driven by agents."""

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"), unique=True)
    stage: Mapped[str] = mapped_column(String(24), default="new", index=True)
    assigned_agent: Mapped[str | None] = mapped_column(String(64))
    offer_price_eur: Mapped[float | None] = mapped_column(Float)
    agreed_price_eur: Mapped[float | None] = mapped_column(Float)
    shipping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{at, by, text}]
    drafts: Mapped[list[Any]] = mapped_column(JSON, default=list)  # outbound message drafts awaiting approval
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    listing: Mapped[Listing] = relationship(back_populates="deal")


class Event(Base):
    """Transactional outbox: domain events consumed by agents (in-process or over HTTP)."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    errors: Mapped[list[Any]] = mapped_column(JSON, default=list)
