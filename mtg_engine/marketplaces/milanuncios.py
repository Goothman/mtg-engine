"""Milanuncios (server-rendered search pages).

The search page embeds its state as JSON (`window.__INITIAL_PROPS__ = JSON.parse("...")`)
and, depending on version, JSON-LD. We try those first and fall back to HTML cards.
Milanuncios sits behind anti-bot protection: if plain HTTP gets blocked, set
`marketplaces.milanuncios.fetcher: playwright` (pip install '.[browser]').
Milanuncios gives no coordinates — only city/province — which the geo resolver maps.

`fetcher: apify` runs an Apify actor instead (default zen-studio/milanuncios-scraper) through
Apify's API, with the token in env APIFY_TOKEN. Its item format isn't documented here, so the
parser accepts the usual field names and keeps the raw item for inspection.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..config import SearchPoint
from ..http import make_client
from .base import Marketplace, RawListing

log = logging.getLogger(__name__)

BASE = "https://www.milanuncios.com"
SEARCH_URL = BASE + "/anuncios/"
_INITIAL_PROPS = re.compile(r"window\.__INITIAL_PROPS__\s*=\s*JSON\.parse\((\".*?\")\);", re.S)
_ID_IN_URL = re.compile(r"-(\d{6,})\.htm")
_PRICE = re.compile(r"(\d[\d.]*(?:,\d{1,2})?)\s*€")


class BlockedError(RuntimeError):
    pass


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("cashPrice", "value", "amount", "price"):
            if k in v:
                return _num(v[k])
        return None
    m = re.search(r"\d[\d.]*(?:,\d{1,2})?", str(v))
    if not m:
        return None
    return float(m.group(0).replace(".", "").replace(",", "."))


def _name(v: Any) -> str | None:
    if isinstance(v, dict):
        return v.get("name") or v.get("label")
    return v or None


def _find_ad_lists(node: Any) -> Iterator[list[dict]]:
    """Recursively find lists that look like ad lists (dicts with id + title)."""
    if isinstance(node, list):
        if node and all(isinstance(x, dict) and "id" in x and "title" in x for x in node[:3]):
            yield node
        for x in node:
            yield from _find_ad_lists(x)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _find_ad_lists(v)


def parse_ad(ad: dict) -> RawListing:
    loc = ad.get("location") or {}
    url = ad.get("url") or ad.get("link") or ""
    images = []
    for img in ad.get("images") or ad.get("photos") or []:
        src = img if isinstance(img, str) else (img.get("src") or img.get("url") or img.get("imageUrl"))
        if src:
            images.append(src if src.startswith("http") else urljoin(BASE, src))
    return RawListing(
        marketplace="milanuncios",
        external_id=str(ad["id"]),
        url=urljoin(BASE, url) if url else f"{BASE}/anuncios/r{ad['id']}.htm",
        title=ad.get("title") or "",
        description=ad.get("description") or ad.get("body") or "",
        price_eur=_num(ad.get("price")),
        city=_name(loc.get("city")) or ad.get("city"),
        region=_name(loc.get("province")) or ad.get("province"),
        postal_code=loc.get("zipCode") or loc.get("postalCode"),
        seller_name=(ad.get("seller") or ad.get("user") or {}).get("name") if isinstance(ad.get("seller") or ad.get("user"), dict) else None,
        images=images,
        raw=ad,
    )


def parse_initial_props(html: str) -> list[RawListing]:
    m = _INITIAL_PROPS.search(html)
    if not m:
        return []
    state = json.loads(json.loads(m.group(1)))
    out: list[RawListing] = []
    seen: set[str] = set()
    for lst in _find_ad_lists(state):
        for ad in lst:
            if str(ad["id"]) in seen:
                continue
            seen.add(str(ad["id"]))
            out.append(parse_ad(ad))
    return out


def parse_html_cards(html: str) -> list[RawListing]:
    tree = HTMLParser(html)
    out: list[RawListing] = []
    for art in tree.css("article"):
        link = art.css_first("a[href*='.htm']")
        if not link:
            continue
        href = link.attributes.get("href") or ""
        m = _ID_IN_URL.search(href)
        if not m:
            continue
        title_node = art.css_first("h2") or art.css_first("[class*='title']") or link
        price_node = art.css_first("[class*='Price']") or art.css_first("[class*='price']")
        loc_node = art.css_first("[class*='Location']") or art.css_first("[class*='location']")
        desc_node = art.css_first("[class*='Description']") or art.css_first("p")
        img = art.css_first("img")
        price_text = price_node.text() if price_node else ""
        pm = _PRICE.search(price_text) or _PRICE.search(art.text())
        loc = loc_node.text(strip=True) if loc_node else None
        city, region = None, None
        if loc:
            parts = [p.strip() for p in re.split(r"[,(]", loc.rstrip(")")) if p.strip()]
            city, region = parts[0], (parts[1] if len(parts) > 1 else None)
        out.append(
            RawListing(
                marketplace="milanuncios",
                external_id=m.group(1),
                url=urljoin(BASE, href),
                title=title_node.text(strip=True),
                description=desc_node.text(strip=True) if desc_node else "",
                price_eur=_num(pm.group(1) + "€") if pm else None,
                city=city,
                region=region,
                images=[img.attributes.get("src") or img.attributes.get("data-src")] if img and (img.attributes.get("src") or img.attributes.get("data-src")) else [],
            )
        )
    return out


def parse_search_page(html: str) -> list[RawListing]:
    lower = html[:5000].lower()
    if ("captcha" in lower or "incapsula" in lower or "datadome" in lower) and "__initial_props__" not in lower:
        raise BlockedError("milanuncios returned an anti-bot page")
    return parse_initial_props(html) or parse_html_cards(html)


def _playwright_proxy(url: str) -> dict:
    from urllib.parse import urlsplit

    u = urlsplit(url)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"}
    if u.username:
        out.update(username=u.username, password=u.password or "")
    return out


APIFY_RUN = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def parse_apify_item(item: dict) -> RawListing | None:
    """One dataset item from the Milanuncios actor -> RawListing (tolerant of field names)."""
    url = _first(item, "url", "link", "adUrl", "detailUrl") or ""
    ext = _first(item, "id", "adId", "ad_id", "listingId")
    if ext is None and url:
        m = _ID_IN_URL.search(url)
        ext = m.group(1) if m else None
    if ext is None:
        return None
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    loc_text = item.get("location") if isinstance(item.get("location"), str) else None
    city = _name(_first(loc, "city", "town", "municipality")) or _first(item, "city", "town")
    province = _name(_first(loc, "province", "region", "state")) or _first(item, "province", "region")
    if loc_text and not (city or province):  # "Ciudad (Provincia)" or "Ciudad, Provincia"
        m = re.match(r"\s*(.*?)\s*[(,]\s*([^)]*)\)?\s*$", loc_text)
        city, province = (m.group(1), m.group(2)) if m else (loc_text, None)
    images = []
    for img in _first(item, "images", "photos", "imageUrls", "pictures") or []:
        src = img if isinstance(img, str) else _first(img, "url", "src", "imageUrl", "large", "original")
        if src:
            images.append(src if str(src).startswith("http") else urljoin(BASE, src))
    if not images and _first(item, "image", "imageUrl", "thumbnail"):
        images = [_first(item, "image", "imageUrl", "thumbnail")]
    seller = _first(item, "seller", "user", "advertiser")
    seller = seller if isinstance(seller, dict) else {"name": seller} if seller else {}
    coords = _first(item, "coordinates", "geo") or {}
    return RawListing(
        marketplace="milanuncios",
        external_id=str(ext),
        url=urljoin(BASE, url) if url else f"{BASE}/anuncios/r{ext}.htm",
        title=_first(item, "title", "name") or "",
        description=_first(item, "description", "body", "text") or "",
        price_eur=_num(_first(item, "price", "priceValue", "amount")),
        city=city,
        region=province,
        postal_code=_first(loc, "zipCode", "postalCode", "zip") or _first(item, "zipCode", "postalCode"),
        lat=_num(_first(coords, "lat", "latitude")) if isinstance(coords, dict) else None,
        lon=_num(_first(coords, "lon", "lng", "longitude")) if isinstance(coords, dict) else None,
        seller_id=str(_first(seller, "id", "userId")) if _first(seller, "id", "userId") else None,
        seller_name=_first(seller, "name", "nickname", "username"),
        images=images,
        raw=item,
    )


class Milanuncios(Marketplace):
    name = "milanuncios"

    def __init__(self, settings, config):
        super().__init__(settings, config)
        self._client = None
        self._results_used = 0  # results requested from the actor in this run
        self._browser = None
        self._pw = None

    def _fetch(self, params: dict) -> str:
        if self.config.fetcher == "playwright":
            if self._browser is None:
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                proxy = self.config.proxy or self.settings.http.proxy
                self._browser = self._pw.chromium.launch(proxy=_playwright_proxy(proxy) if proxy else None)
            page = self._browser.new_page(locale="es-ES", user_agent=self.settings.http.user_agent)
            try:
                from urllib.parse import urlencode

                page.goto(f"{SEARCH_URL}?{urlencode(params)}", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                return page.content()
            finally:
                page.close()
        if self._client is None:
            self._client = make_client(self.settings, self.config.proxy, Accept="text/html,application/xhtml+xml")
        resp = self._client.get(SEARCH_URL, params=params)
        if resp.status_code in (403, 429):
            raise BlockedError(f"milanuncios HTTP {resp.status_code}; try fetcher: playwright or a proxy")
        resp.raise_for_status()
        return resp.text

    def _search_apify(self, query: str) -> Iterator[RawListing]:
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            raise BlockedError("fetcher apify needs the APIFY_TOKEN environment variable")
        if self._client is None:
            self._client = make_client(self.settings, self.config.proxy)
        actor = (self.config.apify_actor or "zen-studio/milanuncios-scraper").replace("/", "~")
        want = self.config.max_results
        if self.config.max_results_per_run is not None:
            want = min(want, self.config.max_results_per_run - self._results_used)
        if want <= 0:
            log.info("milanuncios (apify): per-run budget of %d results used up; skipping %r",
                     self.config.max_results_per_run, query)
            return
        self._results_used += want
        resp = self._client.post(
            APIFY_RUN.format(actor=actor),
            headers={"Authorization": f"Bearer {token}"},
            json={"text": query, "maxResults": want, "sort": "relevance", "isShippable": False},
            timeout=330,  # the sync endpoint waits up to 300 s for the run
        )
        if resp.status_code in (401, 402, 403):
            raise BlockedError(f"apify HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        items = resp.json()
        log.info("milanuncios (apify) q=%r -> %d items", query, len(items))
        for item in items:
            listing = parse_apify_item(item) if isinstance(item, dict) else None
            if listing is not None:
                yield listing

    def search(self, query: str, point: SearchPoint | None = None) -> Iterator[RawListing]:
        if self.config.fetcher == "apify":
            yield from self._search_apify(query)
            return
        for page in range(1, self.config.max_pages + 1):
            params = {"s": query, "orden": "relevance", "fromSearch": 1, "pagina": page}
            ads = parse_search_page(self._fetch(params))
            log.info("milanuncios q=%r page=%d -> %d ads", query, page, len(ads))
            yield from ads
            if not ads:
                break
            time.sleep(self.config.delay_seconds)

    def close(self) -> None:
        if self._client:
            self._client.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
