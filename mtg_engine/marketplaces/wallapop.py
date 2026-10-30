"""Wallapop (internal JSON API used by the web app).

The API is undocumented and changes over time, so parsing is tolerant of both
payload shapes seen so far:
  * newer: {"data": {"section": {"payload": {"items": [...]}}}, "meta": {"next_page": "..."}}
  * older: {"search_objects": [...], "from": .., "to": ..}
Wallapop only returns results to EU/Spanish IPs — set http.proxy if needed.
"""
from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from ..config import SearchPoint
from ..http import get_with_retry, make_client
from .base import Marketplace, RawListing

log = logging.getLogger(__name__)

API_URL = "https://api.wallapop.com/api/v3/search"
USER_URL = "https://api.wallapop.com/api/v3/users/{id}"
ITEM_URL = "https://es.wallapop.com/item/{slug}"


def _ts(v: Any) -> datetime | None:
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, OSError):
        return None


def _price(item: dict) -> float | None:
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount", p.get("cash", {}).get("amount") if isinstance(p.get("cash"), dict) else None)
    if p is None:
        p = item.get("sale_price")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def _images(item: dict) -> list[str]:
    out = []
    for img in item.get("images") or []:
        if isinstance(img, str):
            out.append(img)
            continue
        urls = img.get("urls") or img
        for k in ("big", "medium", "original", "large", "small"):
            if urls.get(k):
                out.append(urls[k])
                break
    return out


def parse_item(item: dict) -> RawListing:
    loc = item.get("location") or {}
    user = item.get("user") or {}
    slug = item.get("web_slug") or item.get("slug") or item.get("id")
    return RawListing(
        marketplace="wallapop",
        external_id=str(item["id"]),
        url=ITEM_URL.format(slug=slug),
        title=item.get("title") or "",
        description=item.get("description") or "",
        price_eur=_price(item),
        city=loc.get("city"),
        region=loc.get("region2") or loc.get("region"),  # region2 = province, region = autonomous community
        postal_code=loc.get("postal_code") or loc.get("zip"),
        country=(loc.get("country_code") or "ES").upper(),
        lat=loc.get("latitude"),
        lon=loc.get("longitude"),
        seller_id=str(item.get("user_id") or user.get("id") or "") or None,
        seller_name=user.get("micro_name") or user.get("name"),
        images=_images(item),
        published_at=_ts(item.get("created_at") or item.get("creation_date")),
        raw=item,
    )


def extract_items(payload: dict) -> tuple[list[dict], str | None]:
    """Return (items, next_page_token) from any known response shape."""
    items: list[dict] = []
    data = payload.get("data")
    if isinstance(data, dict):
        section = data.get("section") or {}
        items = (section.get("payload") or {}).get("items") or section.get("items") or data.get("items") or []
    if not items:
        items = payload.get("search_objects") or payload.get("items") or []
    nxt = (payload.get("meta") or {}).get("next_page")
    return items, nxt


class Wallapop(Marketplace):
    name = "wallapop"

    def __init__(self, settings, config):
        super().__init__(settings, config)
        self.client = make_client(
            settings,
            config.proxy,
            Accept="application/json, text/plain, */*",
            Origin="https://es.wallapop.com",
            Referer="https://es.wallapop.com/",
            **{"X-DeviceOS": "0", "X-AppVersion": "81530"},
        )

    def search(self, query: str, point: SearchPoint | None = None) -> Iterator[RawListing]:
        # Verified live 2026-10-28: "order_by=newest" collapses results (35 -> 1), so we keep
        # Wallapop's default relevance order; the radius parameter is "distance_in_km".
        params: dict[str, Any] = {"keywords": query, "source": "search_box"}
        if point:
            params.update(
                latitude=point.lat,
                longitude=point.lon,
                distance_in_km=self.settings.region.search_radius_km,
            )
        pages = range(self.config.max_pages) if self.config.max_pages is not None else itertools.count()
        for page in pages:
            resp = get_with_retry(self.client, API_URL, params=params)
            items, nxt = extract_items(resp.json())
            log.info("wallapop q=%r point=%s page=%d -> %d items", query, point and point.name, page, len(items))
            for it in items:
                try:
                    yield parse_item(it)
                except (KeyError, TypeError) as e:
                    log.debug("skip malformed wallapop item: %s", e)
            if not items or not nxt:
                break
            params = {"next_page": nxt}
            time.sleep(self.config.delay_seconds)

    def seller_profile(self, seller_id: str) -> dict[str, Any] | None:
        """Name, profile link and reputation; search results only carry the user id."""
        user = get_with_retry(self.client, USER_URL.format(id=seller_id)).json()
        try:
            stats = get_with_retry(self.client, USER_URL.format(id=seller_id) + "/stats").json()
        except Exception as e:  # reputation is a nice-to-have
            log.debug("wallapop stats for %s failed: %s", seller_id, e)
            stats = {}
        counters = {c.get("type"): c.get("value") for c in stats.get("counters") or []}
        seller_type = user.get("seller_type") or {}
        return {
            "name": user.get("micro_name"),
            "url": user.get("url_share", "").split("?")[0] or None,
            "rating": stats.get("rating_average"),
            "reviews": counters.get("reviews"),
            "sold": counters.get("sold"),
            "on_sale": counters.get("publish"),
            "top_profile": user.get("is_top_profile"),
            "verified": seller_type.get("verified"),
            "type": seller_type.get("type"),
            "since": _ts(user.get("register_date")).date().isoformat() if user.get("register_date") else None,
        }

    def close(self) -> None:
        self.client.close()
