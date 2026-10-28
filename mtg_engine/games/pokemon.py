"""Pokémon TCG catalog via pokemontcg.io (includes Cardmarket prices). Shows how to add a game."""
from __future__ import annotations

import os

from ..http import get_with_retry, make_client
from .base import CatalogCard, GameProvider

API = "https://api.pokemontcg.io/v2/cards"


class PokemonProvider(GameProvider):
    name = "pokemon"

    def fetch_set(self, set_code: str) -> list[CatalogCard]:
        headers = {}
        if key := os.environ.get("POKEMONTCG_API_KEY"):
            headers["X-Api-Key"] = key
        out: list[CatalogCard] = []
        with make_client(self.settings, **headers) as client:
            page = 1
            while True:
                data = get_with_retry(client, API, params={"q": f"set.id:{set_code}", "page": page, "pageSize": 250}).json()
                for c in data.get("data", []):
                    cm = (c.get("cardmarket") or {}).get("prices") or {}
                    price = cm.get("trendPrice") or cm.get("averageSellPrice")
                    out.append(
                        CatalogCard(
                            id=f"ptcg-{c['id']}",
                            game="pokemon",
                            set_code=set_code,
                            set_name=c.get("set", {}).get("name", set_code),
                            name=c["name"],
                            collector_number=c.get("number"),
                            rarity=(c.get("rarity") or "").lower() or None,
                            type_line=c.get("supertype"),
                            image_url=(c.get("images") or {}).get("large"),
                            price_eur=float(price) if price else None,
                            price_low_eur=cm.get("lowPrice"),
                            price_source="cardmarket_trend" if price else None,
                        )
                    )
                if page * 250 >= data.get("totalCount", 0):
                    break
                page += 1
        return out
