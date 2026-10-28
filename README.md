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

To publish a read-only snapshot (e.g. on Vercel): `mtg-engine export-static` writes `site/`
(dashboard plus current findings, no backend). `vercel.json` serves `site/` as a static site,
and the Vercel project redeploys on every push to `main`.

**Automatic scans:** `.github/workflows/scan.yml` runs daily (or on demand from the Actions tab,
with mode `all`/`photo`/`text`): catalog sync → scan → seller profiles → agents → `export-static`,
then commits `site/` to `main`, which publishes it. The database is kept between runs on the
`engine-data` branch (one gzip'd commit, overwritten each run).

To run continuously: `mtg-engine scheduler` (catalog sync every 24 h, scans every
`scheduler.interval_minutes`, then an agent pass), or `docker compose up`.

## Configuration (`config/default.yaml`)

| Key | What it controls |
|---|---|
| `games.<game>.targets` | Editions to hunt (`set_code`, label, keywords). Add `2ed` for Unlimited, etc. |
| `games.<game>.queries` | Search terms sent to every marketplace |
| `games.<game>.photo_queries` | Generic searches ("cartas magic"): every ad's photos go through card recognition, even if the text names no card or edition (`mtg-engine scan --photo` runs only these) |
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
- **Photo finds:** for ads from photo queries, a card seen only in the photos counts when the
  model is confident (`vision.photo_min_similarity`, `vision.photo_min_edition_prob`). These ads get
  the `📷 found in photos` chip. The dashboard's search-source filter shows only ads from the
  generic searches (optionally only those where the card was spotted just in the photos); the
  price slider filters by asking price. Only ads with at least one recognised Alpha/Beta card
  are shown (`/api/listings?review=true` also returns "needs review" ads, for agents). Recognitions are cached
  per photo URL and model (`image_scans` table), so rematches don't download images again.
- **Photo checks of text matches:** when an ad's photos can be analysed, a card the model
  confidently sees with a white border (Unlimited, Revised, 4th, 5th…) is vetoed, and if nothing in
  the photos resembles an old card at all the text matches are demoted (`photos_dont_match`).
- **Excluded ads:** `games.<game>.exclude_flags` rule an ad out entirely. By default these are
  other languages: Alpha/Beta were printed only in English, while e.g. Korean and Japanese
  black-bordered reprints look like Beta in photos.
- **Sellers:** for ads shown on the dashboard, the seller's public profile (name, link, rating,
  reviews, items sold, account age) is fetched and shown; `mtg-engine enrich-sellers` backfills it.

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
- **Verified live:** catalog sync from Scryfall and Cardmarket (1,809 printings, 1,803
  priced). Cards are valued at the median of Cardmarket's trend/avg/low/avg1/avg7/avg30,
  because any single field swings wildly for thinly traded Alpha/Beta cards.
- **Marketplace access:** Wallapop's search API (`api.wallapop.com`) only answers EU/Spanish IPs,
  and Milanuncios is behind anti-bot protection. Set a Spanish proxy per marketplace with the
  env vars `MTG_ENGINE_PROXY_WALLAPOP` / `MTG_ENGINE_PROXY_MILANUNCIOS` (or `MTG_ENGINE_PROXY` for
  all traffic). An unreachable marketplace is skipped quickly and reported in the scan history.
- The parsers were written from the sites' public behaviour and tested against hand-written
  fixtures (`tests/fixtures/`). Replace those with real captures on the first live scan.
- Respect each site's terms and rate limits. Delays between pages are configurable.

## Tests

```bash
pytest -q
```
