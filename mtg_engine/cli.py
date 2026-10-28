"""Command-line entry point: `mtg-engine --help`."""
from __future__ import annotations

import json
import logging
import time
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
    config: Optional[str] = ConfigOpt,
):
    """Run one scan across marketplaces and update matches/valuations."""
    from .pipeline import Scanner

    settings, db = _ctx(config)
    stats = Scanner(settings, db, _recognizer(settings)).run(game or None, marketplace or None, max_queries)
    typer.echo(json.dumps(stats))


@app.command()
def rematch(config: Optional[str] = ConfigOpt):
    """Re-run matching and valuation on stored listings (after price/config changes)."""
    from .pipeline import rematch_all

    settings, db = _ctx(config)
    typer.echo(f"re-analysed {rematch_all(settings, db, _recognizer(settings))} listings")


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
