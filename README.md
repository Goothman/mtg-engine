# mtg-engine

Scans second-hand marketplaces for valuable trading cards, values each ad against
Cardmarket prices, ranks the opportunities on a dark dashboard with a map, and hands
promising ads to downstream agents (contact → negotiate → pay → ship).

**Current scope:** Magic: The Gathering **Alpha and Beta**, on **Wallapop** and
**Milanuncios**, in **Spain**. Games, editions, filters, marketplaces and regions are
all set in `config/default.yaml`. See [`ROADMAP.md`](ROADMAP.md) for the architecture and phases.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'            # + '.[vision]' for image recognition, '.[browser]' for Playwright

mtg-engine sync-catalog            # Scryfall printings + Cardmarket price guide
mtg-engine scan --max-queries 2    # quick test scan
mtg-engine agents run              # process events with the built-in agents
mtg-engine serve                   # dashboard on http://127.0.0.1:8000
```

To see the dashboard without network access: `mtg-engine seed-demo` loads demo data
that is labelled as such (made-up ads, placeholder prices). Remove it with `mtg-engine clear-demo`.

To run continuously: `mtg-engine scheduler` (catalog sync every 24 h, scans every
`scheduler.interval_minutes`, then an agent pass), or `docker compose up`.

## Configuration (`config/default.yaml`)

| Key | What it controls |
|---|---|
| `games.<game>.targets` | Editions to hunt (`set_code`, label, keywords). Add `2ed` for Unlimited, etc. |
| `games.<game>.queries` | Search terms sent to every marketplace |
| `games.<game>.filters` | Minimum card and ad value, maximum asking price, rarities, card types, basic lands |
| `games.<game>.red_flags` | Words that halve match confidence (proxy, réplica, unlimited…) |
| `marketplaces.*` | Enable/disable, pages, delay, `fetcher: playwright` for anti-bot pages |
| `region` | Country, map centre, Wallapop search points + radius |
| `pricing` | Source order, USD→EUR rate |
| `vision` | Turn on image recognition once a model is trained (see `ml/README.md`) |
| `deals` | Thresholds for opening a deal automatically |

`MTG_ENGINE_CONFIG=other.yaml` switches the config file. `MTG_ENGINE_DATABASE_URL=postgresql+psycopg://…`
switches the database.

### Adding things
- **A marketplace:** subclass `Marketplace` (`mtg_engine/marketplaces/base.py`), yield
  `RawListing`s, register it in `marketplaces/__init__.py`, and add a config block.
- **A game:** subclass `GameProvider` (see `games/pokemon.py`), register it, and add a `games.<name>` block.
- **A country:** add `mtg_engine/geo/data/<cc>.json` (regions with centroids and aliases) and search points.

## How matching and valuation work
- Card names are matched on word boundaries in accent-normalised text. Quantities are
  read as `4x Name` or `Name x4`. Alpha/Beta keywords set the edition. Generic names
  (Fog, Island…) need edition evidence.
- If the ad names both editions, or neither, the cheaper printing is used and the
  match is marked *ambiguous*. If no edition is named at all, the match gets low
  confidence and is left out of the total.
- **Total value** = Σ qty × Cardmarket trend price, counting matches with confidence ≥ 0.5.
  **Ratio** = value / asking price. Confirming or rejecting a match in the dashboard
  recalculates both.
- Ads that name an edition but no identifiable cards are marked **needs review**. Vision
  or a human handles those.

## Agents and integration
Every change produces an event in the `events` outbox table: `listing.discovered`,
`listing.matched`, `deal.opened`, `deal.stage_changed`, `deal.draft_created`. Deals
move through a checked state machine:
`new → shortlisted → contacted → negotiating → agreed → paid → shipped → received` (plus `rejected` and `cancelled`).

- **In-process:** subclass `mtg_engine.agents.Agent`, set `subscribes`, implement
  `handle()`, and add it to the runner.
- **External** (any language, or an LLM agent): use the HTTP API:
  - `GET /api/events?agent=<name>&types=deal.opened` claims events (at-least-once; reclaimed after 10 min)
  - `POST /api/events/{id}/ack` with `{}` or `{"error": "..."}`
  - `GET /api/deals`, `POST /api/deals/{id}/transition` with `{"to": "contacted", "by": "agent:x", "fields": {"offer_price_eur": 120}}`
  - `POST /api/deals/{id}/notes`, `GET /api/listings/{id}`

Built-in agents are safe stubs. `OutreachAgent` writes a Spanish first-contact
message for a human to approve; neither marketplace has a public messaging API.
`NegotiationAgent` suggests an opening offer. `LogisticsAgent` is a placeholder.

## Status and caveats
- The Wallapop and Milanuncios clients follow the sites' current internal JSON and
  HTML. They were written **without live access**: the sandbox they were built in
  blocks those hosts, so the parsers are tested against hand-written fixtures
  (`tests/fixtures/`). Phase 1 of the roadmap checks them against live responses.
  Wallapop returns nothing to non-EU IPs (set `http.proxy`). Milanuncios may need
  `fetcher: playwright`.
- Respect each site's terms and rate limits. Delays between pages are configurable.
- Vision is off until a model is trained (`ml/`).

## Tests

```bash
pytest -q
```
