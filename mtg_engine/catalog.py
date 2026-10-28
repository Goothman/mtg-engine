"""Catalog + price sync: provider -> card_printings table."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from .config import Settings
from .games import CatalogCard, get_provider
from .models import CardPrinting, utcnow
from .text import normalize

log = logging.getLogger(__name__)


def upsert_cards(session: Session, cards: list[CatalogCard], target_codes: set[str]) -> int:
    now = utcnow()
    for c in cards:
        row = session.get(CardPrinting, c.id) or CardPrinting(id=c.id)
        row.game = c.game
        row.set_code = c.set_code
        row.set_name = c.set_name
        row.name = c.name
        row.name_norm = normalize(c.name)
        row.collector_number = c.collector_number
        row.rarity = c.rarity
        row.type_line = c.type_line
        row.image_url = c.image_url
        row.cardmarket_id = c.cardmarket_id
        row.is_target = c.set_code in target_codes
        row.price_eur = c.price_eur
        row.price_low_eur = c.price_low_eur
        row.price_source = c.price_source
        row.price_updated_at = now
        row.extra = c.extra
        session.merge(row)
    return len(cards)


def sync_catalog(settings: Settings, session: Session, game: str, include_confusers: bool = True) -> dict[str, int]:
    gcfg = settings.games[game]
    provider = get_provider(game, settings)
    sets = list(gcfg.target_codes) + (list(gcfg.confuser_sets) if include_confusers else [])
    cards: list[CatalogCard] = []
    for code in sets:
        cards.extend(provider.fetch_set(code))
    provider.apply_external_prices(cards)
    n = upsert_cards(session, cards, set(gcfg.target_codes))
    stats = {code: sum(1 for c in cards if c.set_code == code) for code in sets}
    stats["total"] = n
    stats["priced"] = sum(1 for c in cards if c.price_eur)
    log.info("catalog sync %s: %s", game, stats)
    return stats
