"""Typed configuration loaded from YAML."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"


class HttpConfig(BaseModel):
    user_agent: str = "Mozilla/5.0"
    timeout_seconds: float = 30
    proxy: str | None = None


class SearchPoint(BaseModel):
    name: str
    lat: float
    lon: float


class RegionConfig(BaseModel):
    country: str = "ES"
    map_center: tuple[float, float] = (40.2, -3.7)
    map_zoom: int = 6
    search_radius_km: int = 200
    search_points: list[SearchPoint] = Field(default_factory=list)


class MarketplaceConfig(BaseModel):
    enabled: bool = True
    max_pages: int = 3
    delay_seconds: float = 2.0
    fetcher: str = "httpx"
    use_search_points: bool = False
    # Per-marketplace proxy (e.g. a Spanish exit IP for Wallapop). Prefer setting it via the
    # env var MTG_ENGINE_PROXY_<NAME> (e.g. MTG_ENGINE_PROXY_WALLAPOP) to keep credentials out of git.
    proxy: str | None = None
    # fetcher "apify": an Apify actor does the fetching (token in env APIFY_TOKEN, never in git)
    apify_actor: str | None = None
    max_results: int = 100  # per query, for actor-based fetchers
    max_results_per_run: int | None = None  # total budget per scan run (billed per result)
    category_slugs: list[str] = Field(default_factory=list)  # keep only ads in these categories
    query_kinds: list[str] = Field(default_factory=lambda: ["photo", "text"])  # which game queries to run here


class TargetSet(BaseModel):
    set_code: str
    label: str
    keywords: list[str] = Field(default_factory=list)


class Filters(BaseModel):
    min_card_value_eur: float = 0
    min_listing_value_eur: float = 0
    max_listing_price_eur: float | None = None
    rarities: list[str] = Field(default_factory=list)
    type_contains: list[str] = Field(default_factory=list)
    exclude_basic_lands: bool = False


class GameConfig(BaseModel):
    enabled: bool = True
    queries: list[str] = Field(default_factory=list)
    # Broad searches ("cartas magic") whose ads are checked photo by photo with the vision model,
    # even when the text names no edition or card.
    photo_queries: list[str] = Field(default_factory=list)
    targets: list[TargetSet] = Field(default_factory=list)
    confuser_sets: list[str] = Field(default_factory=list)
    filters: Filters = Field(default_factory=Filters)
    red_flags: list[str] = Field(default_factory=list)
    # Words that rule an ad out entirely (e.g. other languages: Alpha/Beta were printed in English only).
    exclude_flags: list[str] = Field(default_factory=list)

    @property
    def target_codes(self) -> list[str]:
        return [t.set_code for t in self.targets]


class PricingConfig(BaseModel):
    currency: str = "EUR"
    usd_to_eur: float = 0.92
    sources: list[str] = Field(default_factory=lambda: ["cardmarket_price_guide", "scryfall"])


class VisionConfig(BaseModel):
    enabled: bool = False
    model_path: str = "models/card_model.onnx"
    gallery_path: str = "models/gallery.npz"
    min_similarity: float = 0.55
    max_images_per_listing: int | None = None  # None = every photo (a valuable card can be in any of them)
    # Stricter bar for matches found only in photos (no supporting text): generic lots are
    # mostly modern cards, and the model has only seen Alpha/Beta and their look-alike editions.
    photo_min_similarity: float = 0.7
    photo_min_edition_prob: float = 0.7
    # A photo that confidently shows a white border (Unlimited/Revised/4th/5th...) vetoes that card.
    white_border_veto: float = 0.5
    # If an ad's photos were checked and nothing in them resembles an old card, text matches
    # are demoted (e.g. "Myr alfa" is a modern card, not Alpha).
    photo_contradiction_similarity: float = 0.45


class DealsConfig(BaseModel):
    auto_open_min_value_eur: float = 25
    auto_open_min_ratio: float = 1.2


class MapConfig(BaseModel):
    tiles: str = "osm_dark"  # osm_dark | osm | carto_dark | none
    boundaries: str | None = "es-provinces.geojson"  # bundled vector layer (works without tiles)


class SchedulerConfig(BaseModel):
    interval_minutes: int = 60


class Settings(BaseModel):
    database_url: str = "sqlite:///data/mtg_engine.db"
    http: HttpConfig = Field(default_factory=HttpConfig)
    region: RegionConfig = Field(default_factory=RegionConfig)
    marketplaces: dict[str, MarketplaceConfig] = Field(default_factory=dict)
    games: dict[str, GameConfig] = Field(default_factory=dict)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    deals: DealsConfig = Field(default_factory=DealsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    map: MapConfig = Field(default_factory=MapConfig)

    def enabled_games(self) -> dict[str, GameConfig]:
        return {k: v for k, v in self.games.items() if v.enabled}

    def enabled_marketplaces(self) -> dict[str, MarketplaceConfig]:
        return {k: v for k, v in self.marketplaces.items() if v.enabled}

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path


def load_settings(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    path = Path(path or os.environ.get("MTG_ENGINE_CONFIG") or DEFAULT_CONFIG)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if url := os.environ.get("MTG_ENGINE_DATABASE_URL"):
        data["database_url"] = url
    if overrides:
        data.update(overrides)
    settings = Settings.model_validate(data)
    if p := os.environ.get("MTG_ENGINE_PROXY"):
        settings.http.proxy = p
    for name, mcfg in settings.marketplaces.items():
        if p := os.environ.get(f"MTG_ENGINE_PROXY_{name.upper()}"):
            mcfg.proxy = p
        if n := os.environ.get(f"MTG_ENGINE_MAX_RESULTS_{name.upper()}"):  # one-off full crawls
            mcfg.max_results, mcfg.max_results_per_run = int(n), None
    return settings


@lru_cache
def get_settings() -> Settings:
    return load_settings()
