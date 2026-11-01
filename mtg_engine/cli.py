"""Command-line entry point: `mtg-engine --help`."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import typer

from .config import load_settings
from .db import Database

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Second-hand card deal scanner.")
agents_app = typer.Typer(help="Downstream agents.")
app.add_typer(agents_app, name="agents")


def _ctx(config: Optional[str]):
    settings = load_settings(config)
    db = Database(settings)
    db.create_all()
    return settings, db


def _recognizer(settings):
    if not settings.vision.enabled:
        return None
    model, gallery = settings.resolve_path(settings.vision.model_path), settings.resolve_path(settings.vision.gallery_path)
    if not (model.exists() and gallery.exists()):
        logging.getLogger("vision").warning(
            "vision enabled but %s / %s missing (train with ml/, see ml/README.md). Continuing with text matching only.", model, gallery)
        return None
    try:
        from .vision.service import load_recognizer
    except ImportError as e:
        logging.getLogger("vision").warning("vision extras not installed (%s): pip install '.[vision]'", e)
        return None
    return load_recognizer(settings)


ConfigOpt = typer.Option(None, "--config", "-c", help="YAML config path (default config/default.yaml)")


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command("init-db")
def init_db(config: Optional[str] = ConfigOpt):
    """Create database tables."""
    _ctx(config)
    typer.echo("database ready")


@app.command("sync-catalog")
def sync_catalog_cmd(game: list[str] = typer.Option(None, "--game", "-g"), no_confusers: bool = False, config: Optional[str] = ConfigOpt):
    """Download target-set catalogs and prices (Scryfall + Cardmarket price guide)."""
    from .catalog import sync_catalog

    settings, db = _ctx(config)
    for g in game or list(settings.enabled_games()):
        with db.session() as s:
            typer.echo(f"{g}: {json.dumps(sync_catalog(settings, s, g, include_confusers=not no_confusers))}")


@app.command()
def scan(
    game: list[str] = typer.Option(None, "--game", "-g"),
    marketplace: list[str] = typer.Option(None, "--marketplace", "-m"),
    max_queries: Optional[int] = typer.Option(None, help="Only run the first N queries (smoke test)"),
    photo: Optional[bool] = typer.Option(None, "--photo/--no-photo",
                                         help="--photo: only photo queries (vision on every ad); --no-photo: only text queries"),
    defer_photos: bool = typer.Option(False, "--defer-photos",
                                      help="Store and text-match new ads now; read their photos later with rematch --keep-photos"),
    max_minutes: Optional[float] = typer.Option(None, help="Stop cleanly after this long (results so far are kept)"),
    config: Optional[str] = ConfigOpt,
):
    """Run one scan across marketplaces and update matches/valuations."""
    from .pipeline import Scanner

    settings, db = _ctx(config)
    recognizer = _recognizer(settings)
    if photo and recognizer is None:
        raise typer.BadParameter("--photo needs the vision model (models/) and pip install '.[vision]'")
    scanner = Scanner(settings, db, recognizer)
    scanner.defer_photos = defer_photos
    if max_minutes:
        import time

        scanner.deadline = time.monotonic() + max_minutes * 60
    stats = scanner.run(game or None, marketplace or None, max_queries, photo)
    typer.echo(json.dumps(stats))


@app.command()
def rematch(max_minutes: Optional[float] = typer.Option(None, help="Stop after this long (resume on the next run)"),
            keep_photos: bool = typer.Option(False, "--keep-photos",
                                             help="Reuse stored photo results; only photos never read are downloaded"),
            config: Optional[str] = ConfigOpt):
    """Re-run matching and valuation on stored listings (after price/config/model changes)."""
    from .pipeline import rematch_all

    settings, db = _ctx(config)
    recognizer = _recognizer(settings)  # with --keep-photos it only reads stored photo results
    typer.echo(f"re-analysed {rematch_all(settings, db, recognizer, max_minutes, keep_photos=keep_photos)} listings")


@app.command("text-check-replay")
def text_check_replay(out: str = typer.Option("text-check-replay.json", help="Report file"),
                      config: Optional[str] = ConfigOpt):
    """Apply the photo text check to every ad on the dashboard and report what changes (before publishing)."""
    from sqlalchemy import or_, select

    from .http import make_client
    from .models import Listing, TextCheck
    from .pipeline import analyse, build_matcher

    settings, db = _ctx(config)
    settings.vision.text_check = True  # what the dashboard would look like with it on
    recognizer = _recognizer(settings)
    if recognizer is None:
        raise typer.BadParameter("needs the vision model (models/) and pip install '.[vision]'")
    report = []
    with db.session() as s, make_client(settings) as client:
        ads = s.scalars(select(Listing).where(Listing.status == "active", or_(Listing.match_count > 0, Listing.needs_review))).all()
        matchers = {}
        for listing in ads:
            before = (listing.match_count, listing.est_total_value, sorted(m.card.name for m in listing.matches if m.confidence > 0))
            m = matchers.get(listing.game) or matchers.setdefault(listing.game, build_matcher(s, settings, listing.game))
            analyse(s, settings, listing, m, recognizer, client, keep_vision=True)
            s.flush()
            checks = s.scalars(select(TextCheck).where(TextCheck.url.in_(listing.images or []))).all()
            report.append({
                "id": listing.id, "title": listing.title, "url": listing.url, "price": listing.price_eur,
                "before": {"shown": before[0] > 0, "value": before[1], "cards": before[2]},
                "after": {"shown": listing.match_count > 0, "value": listing.est_total_value,
                          "cards": sorted(m.card.name for m in listing.matches if m.confidence > 0),
                          "flags": [f for f in listing.flags or [] if f.startswith(("vision:text", "text_"))]},
                "checks": [{"photo": c.url, "card": c.card_name, **c.result} for c in checks],
            })
            typer.echo(f"{listing.id}: {'shown' if before[0] else '-'} -> {'shown' if listing.match_count else 'HIDDEN'} "
                       f"{[(c.card_name, c.result.get('decision'), c.result.get('reason')) for c in checks]}", err=True)
    Path(out).write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    gone = sum(r["before"]["shown"] and not r["after"]["shown"] for r in report)
    typer.echo(json.dumps({"ads": len(report), "hidden": gone, "checked": sum(len(r["checks"]) for r in report)}))


@app.command("import-ads")
def import_ads(path: str, game: str = "mtg", photo: bool = typer.Option(True, "--photo/--no-photo",
               help="Check every ad's photos (as for a generic search)"),
               config: Optional[str] = ConfigOpt):
    """Analyse ads scraped elsewhere: Apify milanuncios items, a page-scraper export (ad_title...)
    or a gallery export ({"listings": [{external_id, url, title, price_text, images}]})."""
    from .marketplaces.milanuncios import parse_apify_item, parse_export_item, parse_listing_item
    from .pipeline import import_listings

    settings, db = _ctx(config)
    items = json.loads(open(path, encoding="utf-8").read())
    if isinstance(items, dict):
        items = items.get("listings") or items.get("items") or []

    def parse(it: dict):
        if "ad_title" in it:
            return parse_export_item(it)
        if "external_id" in it:
            return parse_listing_item(it)
        return parse_apify_item(it)

    raws = [r for it in items if isinstance(it, dict) for r in [parse(it)] if r is not None]
    typer.echo(f"{len(raws)}/{len(items)} ads parsed, {sum(len(r.images) for r in raws)} photos")
    recognizer = _recognizer(settings) if photo else None
    typer.echo(json.dumps(import_listings(settings, db, recognizer, raws, game, photo=photo)))


@app.command("enrich-sellers")
def enrich_sellers_cmd(config: Optional[str] = ConfigOpt):
    """Fetch seller names and reputation for the ads shown on the dashboard."""
    from .pipeline import enrich_sellers

    settings, db = _ctx(config)
    typer.echo(f"filled {enrich_sellers(settings, db)} seller profiles")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, config: Optional[str] = ConfigOpt):
    """Serve the API and dashboard."""
    import uvicorn

    from .api import create_app

    settings, db = _ctx(config)
    uvicorn.run(create_app(settings, db), host=host, port=port)


@app.command()
def scheduler(sync_catalog_every_hours: int = 24, run_agents: bool = True, config: Optional[str] = ConfigOpt):
    """Loop forever: periodic catalog/price sync, scans and agent passes."""
    from .catalog import sync_catalog
    from .pipeline import Scanner

    settings, db = _ctx(config)
    recognizer = _recognizer(settings)
    last_sync = 0.0
    log = logging.getLogger("scheduler")
    while True:
        if time.time() - last_sync > sync_catalog_every_hours * 3600:
            for g in settings.enabled_games():
                try:
                    with db.session() as s:
                        sync_catalog(settings, s, g)
                except Exception:
                    log.exception("catalog sync failed for %s", g)
            last_sync = time.time()
        try:
            log.info("scan: %s", Scanner(settings, db, recognizer).run())
        except Exception:
            log.exception("scan failed")
        if run_agents:
            _run_agents_once(settings, db)
        time.sleep(settings.scheduler.interval_minutes * 60)


def _run_agents_once(settings, db) -> int:
    from .agents import DEFAULT_AGENTS, AgentContext, AgentRunner, DraftOnlyMessaging

    with db.session() as s:
        return AgentRunner(DEFAULT_AGENTS).run_once(AgentContext(s, settings, DraftOnlyMessaging()))


@agents_app.command("run")
def agents_run(loop: bool = False, interval: int = 30, config: Optional[str] = ConfigOpt):
    """Process pending outbox events with the built-in agents."""
    settings, db = _ctx(config)
    while True:
        typer.echo(f"processed {_run_agents_once(settings, db)} events")
        if not loop:
            break
        time.sleep(interval)


@app.command("export-static")
def export_static(out: str = "site", config: Optional[str] = ConfigOpt):
    """Export the dashboard + current findings as a static, read-only site (e.g. for Vercel)."""
    from .static_export import export_site

    settings, db = _ctx(config)
    typer.echo(export_site(settings, db, out))


@app.command("seed-demo")
def seed_demo_cmd(config: Optional[str] = ConfigOpt):
    """Insert clearly-labelled DEMO data (fake ads, illustrative prices) to preview the dashboard."""
    from .demo import seed_demo

    settings, db = _ctx(config)
    with db.session() as s:
        typer.echo(f"seeded {seed_demo(s, settings)} demo listings")


@app.command("clear-demo")
def clear_demo_cmd(config: Optional[str] = ConfigOpt):
    """Remove all demo data."""
    from .demo import clear_demo

    settings, db = _ctx(config)
    with db.session() as s:
        typer.echo(f"removed {clear_demo(s)} demo listings")


if __name__ == "__main__":
    app()
