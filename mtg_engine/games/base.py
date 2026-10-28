"""Game plugin interface: a game knows how to fetch its card catalog and prices."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import GameConfig, Settings


@dataclass
class CatalogCard:
    id: str
    game: str
    set_code: str
    set_name: str
    name: str
    collector_number: str | None = None
    rarity: str | None = None
    type_line: str | None = None
    image_url: str | None = None
    cardmarket_id: int | None = None
    price_eur: float | None = None
    price_low_eur: float | None = None
    price_source: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class GameProvider(ABC):
    name: str

    def __init__(self, settings: Settings, config: GameConfig):
        self.settings = settings
        self.config = config

    @abstractmethod
    def fetch_set(self, set_code: str) -> list[CatalogCard]:
        """Return every printing in a set, with whatever price the source provides."""

    def apply_external_prices(self, cards: list[CatalogCard]) -> None:  # noqa: B027
        """Optional: overwrite/complete prices from a better source (e.g. Cardmarket guide)."""

    def is_basic_land(self, card: CatalogCard) -> bool:
        return False
