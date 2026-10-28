"""Cardmarket public daily price guide (free, no API key).

Cardmarket publishes a JSON price guide per game, keyed by Cardmarket product id
(`idProduct`). Scryfall exposes the same id as `cardmarket_id`, which is how we join.
Game ids: 1 = Magic, 6 = Pokémon, 3 = Yu-Gi-Oh!.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import ROOT

log = logging.getLogger(__name__)

PRICE_GUIDE_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_{game_id}.json"
CACHE_DIR = ROOT / "data" / "cache"


@dataclass
class GuidePrice:
    trend: float | None
    avg: float | None
    low: float | None
    avg30: float | None
    avg1: float | None = None
    avg7: float | None = None

    def best(self) -> float | None:
        """Median of all available price fields.

        Old cards trade rarely and Cardmarket mixes conditions, so any single field can be
        far off (e.g. an Alpha Island with trend 1403 EUR vs low 32 EUR). The median of
        trend/avg/low/avg1/avg7/avg30 is much more stable.
        """
        vals = sorted(float(v) for v in (self.trend, self.avg, self.low, self.avg1, self.avg7, self.avg30) if v)
        if not vals:
            return None
        mid = len(vals) // 2
        return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 2)


def parse_price_guide(data: dict) -> dict[int, GuidePrice]:
    out: dict[int, GuidePrice] = {}
    for row in data.get("priceGuides", []):
        pid = row.get("idProduct")
        if pid is None:
            continue
        out[int(pid)] = GuidePrice(
            trend=row.get("trend"), avg=row.get("avg"), low=row.get("low"), avg30=row.get("avg30"),
            avg1=row.get("avg1"), avg7=row.get("avg7"),
        )
    return out


def load_price_guide(client: httpx.Client, game_id: int = 1, use_cache: bool = True) -> dict[int, GuidePrice]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"cm_price_guide_{game_id}.json"
    try:
        resp = client.get(PRICE_GUIDE_URL.format(game_id=game_id))
        resp.raise_for_status()
        cache.write_bytes(resp.content)
        data = resp.json()
    except Exception as e:  # network failure: fall back to the last copy we downloaded
        if use_cache and cache.exists():
            log.warning("Cardmarket price guide download failed (%s); using cached %s", e, cache)
            data = json.loads(Path(cache).read_text())
        else:
            raise
    return parse_price_guide(data)
