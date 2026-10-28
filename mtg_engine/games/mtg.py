"""Magic: The Gathering catalog via Scryfall (free API, ~10 req/s allowed)."""
from __future__ import annotations

import logging
import time

from ..http import get_with_retry, make_client
from ..pricing.cardmarket import load_price_guide
from .base import CatalogCard, GameProvider

log = logging.getLogger(__name__)

SCRYFALL = "https://api.scryfall.com"
BASIC_LANDS = {"Plains", "Island", "Swamp", "Mountain", "Forest"}


def scryfall_to_card(c: dict, usd_to_eur: float) -> CatalogCard:
    prices = c.get("prices") or {}
    image = (c.get("image_uris") or {}).get("large")
    if not image and c.get("card_faces"):
        image = (c["card_faces"][0].get("image_uris") or {}).get("large")
    eur = prices.get("eur")
    usd = prices.get("usd")
    price, source = None, None
    if eur:
        price, source = float(eur), "scryfall_eur"
    elif usd:
        price, source = round(float(usd) * usd_to_eur, 2), "scryfall_usd_fx"
    return CatalogCard(
        id=c["id"],
        game="mtg",
        set_code=c["set"],
        set_name=c.get("set_name", c["set"]),
        name=c["name"],
        collector_number=c.get("collector_number"),
        rarity=c.get("rarity"),
        type_line=c.get("type_line"),
        image_url=image,
        cardmarket_id=c.get("cardmarket_id"),
        price_eur=price,
        price_source=source,
        extra={"oracle_id": c.get("oracle_id"), "usd": usd, "eur": eur},
    )


class MtgProvider(GameProvider):
    name = "mtg"

    def fetch_set(self, set_code: str) -> list[CatalogCard]:
        cards: list[CatalogCard] = []
        with make_client(self.settings, Accept="application/json") as client:
            url: str | None = f"{SCRYFALL}/cards/search"
            params: dict | None = {"q": f"set:{set_code}", "unique": "prints", "order": "set"}
            while url:
                data = get_with_retry(client, url, params=params).json()
                cards.extend(scryfall_to_card(c, self.settings.pricing.usd_to_eur) for c in data.get("data", []))
                url = data.get("next_page") if data.get("has_more") else None
                params = None  # next_page already carries the query
                time.sleep(0.12)  # Scryfall asks for 50-100 ms between requests
        log.info("scryfall: %s -> %d printings", set_code, len(cards))
        return cards

    def apply_external_prices(self, cards: list[CatalogCard]) -> None:
        if "cardmarket_price_guide" not in self.settings.pricing.sources:
            return
        try:
            with make_client(self.settings) as client:
                guide = load_price_guide(client, game_id=1)
        except Exception as e:
            log.warning("Cardmarket price guide unavailable (%s); keeping Scryfall prices", e)
            return
        hits = 0
        for card in cards:
            gp = guide.get(card.cardmarket_id) if card.cardmarket_id else None
            if gp and gp.best():
                card.price_eur = gp.best()
                card.price_low_eur = gp.low
                card.price_source = "cardmarket_trend"
                hits += 1
        log.info("cardmarket guide priced %d/%d cards", hits, len(cards))

    def is_basic_land(self, card: CatalogCard) -> bool:
        return card.name in BASIC_LANDS
