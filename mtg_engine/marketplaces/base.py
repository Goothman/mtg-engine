"""Marketplace plugin interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import MarketplaceConfig, SearchPoint, Settings


@dataclass
class RawListing:
    marketplace: str
    external_id: str
    url: str
    title: str
    description: str = ""
    price_eur: float | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str = "ES"
    lat: float | None = None
    lon: float | None = None
    seller_id: str | None = None
    seller_name: str | None = None
    images: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.marketplace}:{self.external_id}"


class Marketplace(ABC):
    name: str

    def __init__(self, settings: Settings, config: MarketplaceConfig):
        self.settings = settings
        self.config = config

    @abstractmethod
    def search(self, query: str, point: SearchPoint | None = None) -> Iterator[RawListing]:
        """Yield listings for a keyword query, paginating up to config.max_pages."""

    def close(self) -> None:  # noqa: B027
        pass
