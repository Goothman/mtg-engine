"""Resolve listing locations to map coordinates.

Exact coordinates from the marketplace win. Otherwise we fall back to a per-country
gazetteer (province/city -> centroid), with a small deterministic jitter so that
several ads in the same province don't stack on one pin. Add a country by dropping
`data/<cc>.json` in the same format.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from ..text import normalize

DATA = Path(__file__).parent / "data"


@lru_cache
def _gazetteer(country: str) -> dict[str, tuple[float, float, str]]:
    path = DATA / f"{country.lower()}.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    index: dict[str, tuple[float, float, str]] = {}
    for p in data["provinces"]:
        for n in [p["name"], *p.get("aliases", [])]:
            index[normalize(n)] = (p["lat"], p["lon"], p["name"])
    return index


def _jitter(key: str, scale: float = 0.08) -> tuple[float, float]:
    h = hashlib.sha1(key.encode()).digest()
    return ((h[0] / 255 - 0.5) * scale, (h[1] / 255 - 0.5) * scale)


def resolve(
    country: str, lat: float | None, lon: float | None, city: str | None, region: str | None, key: str = ""
) -> tuple[float | None, float | None, str, str | None]:
    """Return (lat, lon, precision, province_name)."""
    gaz = _gazetteer(country)
    province = None
    for cand in (region, city):
        if cand and (hit := gaz.get(normalize(cand))):
            province = hit[2]
            break
    if lat is not None and lon is not None:
        return lat, lon, "exact", province
    for cand, precision in ((city, "city"), (region, "province")):
        if cand and (hit := gaz.get(normalize(cand))):
            dy, dx = _jitter(key)
            return hit[0] + dy, hit[1] + dx, precision, hit[2]
    return None, None, "none", province
