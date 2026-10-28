"""Static snapshot of the dashboard: same HTML/JS, data embedded, no backend needed."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from .api.app import WEB, create_app, listing_out
from .config import ROOT, Settings
from .db import Database
from .models import Listing, utcnow


def export_site(settings: Settings, db: Database, out: str) -> str:
    dest = Path(out) if Path(out).is_absolute() else ROOT / out
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(WEB, dest / "static", ignore=shutil.ignore_patterns("index.html"))

    client = TestClient(create_app(settings, db))  # reuse the API serialisers exactly
    with db.session() as s:
        # only ads with at least one recognised Alpha/Beta card are published
        rows = s.scalars(select(Listing).where(Listing.status == "active", Listing.match_count > 0))
        listings = [listing_out(l) for l in rows]
    data = {
        "generated_at": utcnow().isoformat(),
        "config": client.get("/api/config").json(),
        "stats": client.get("/api/stats").json(),
        "deals": client.get("/api/deals").json(),
        "events": client.get("/api/events?limit=100").json(),
        "listings": json.loads(json.dumps(listings, default=str)),
    }
    for l in data["listings"]:  # keep the page light: raw payloads aren't used by the UI
        l.pop("raw", None)
    html = (WEB / "index.html").read_text(encoding="utf-8").replace('"/static/', '"static/')
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace('<script src="static/app.js"></script>',
                        f'<script>window.MTG_STATIC = {payload};</script>\n  <script src="static/app.js"></script>')
    (dest / "index.html").write_text(html, encoding="utf-8")
    return f"wrote {dest} ({len(listings)} listings, {(dest / 'index.html').stat().st_size // 1024} KB)"
