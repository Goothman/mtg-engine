# mtg-engine — Plan & Roadmap

Find underpriced trading cards on second-hand marketplaces, value them against
reference prices, rank the opportunities, and hand them off to downstream agents
(contact → negotiate → pay → ship).

Initial scope: **Magic: The Gathering, Alpha (LEA) and Beta (LEB)**, scanned on
**Wallapop** and **Milanuncios** in **Spain**. Everything outside that is
configuration or plugins, not rewrites.

---

## 1. Architecture

```
                 ┌────────────── config/default.yaml ──────────────┐
                 │ games · target editions · filters · marketplaces │
                 │ region / search points · pricing · vision       │
                 └───────────────────────┬─────────────────────────┘
                                         │
  ┌──────────────┐   ┌───────────────┐   │   ┌────────────────────┐
  │ Game plugins │   │ Marketplace   │   │   │ Price sources      │
  │  mtg (Scryfall)  │ plugins       │   │   │  Cardmarket guide  │
  │  pokemon (TCG API)│ wallapop     │   │   │  Scryfall (EUR/USD)│
  │  …               │ milanuncios   │   │   │  pokemontcg.io     │
  └──────┬───────┘   └──────┬────────┘   │   └─────────┬──────────┘
         │ catalog          │ listings   │             │ prices
         ▼                  ▼            ▼             ▼
  ┌──────────────────────────────────────────────────────────────┐
  │                     Scan pipeline (runner)                   │
  │ fetch → normalise → upsert → match (text + vision) → value   │
  │ → geo-resolve → rank → emit domain events                    │
  └───────────────┬──────────────────────────────┬───────────────┘
                  ▼                              ▼
        ┌───────────────────┐          ┌───────────────────────┐
        │ SQL DB (SQLite →  │          │ Event outbox + Deals  │
        │ Postgres)         │          │ (state machine)       │
        └────────┬──────────┘          └──────────┬────────────┘
                 ▼                                ▼
        ┌───────────────────┐          ┌───────────────────────┐
        │ FastAPI + dark    │          │ Agents (pluggable):   │
        │ dashboard + map   │          │ outreach · negotiation│
        └───────────────────┘          │ · payment · logistics │
                                       └───────────────────────┘
```

### Main parts

| Component | Location | Notes |
|---|---|---|
| Config | `config/default.yaml`, `mtg_engine/config.py` | YAML file; `MTG_ENGINE_CONFIG` env var picks another file |
| Game plugins | `mtg_engine/games/` | `GameProvider`: catalog + keyword hints. `mtg` (Scryfall), `pokemon` (pokemontcg.io) |
| Price sources | `mtg_engine/pricing/` | Cardmarket daily price guide (free public JSON), Scryfall fallback, USD→EUR fallback |
| Marketplaces | `mtg_engine/marketplaces/` | `Marketplace.search(query, point) -> [RawListing]`; registry by name |
| Matching | `mtg_engine/matching/` | Text matcher (names, quantities, edition keywords, red flags) + optional vision |
| Vision | `mtg_engine/vision/` | Card quad detection (OpenCV) → ONNX embedder → gallery kNN; edition head |
| Valuation | `mtg_engine/valuation.py` | Total value per ad, value/price ratio, confidence-weighted score |
| Geo | `mtg_engine/geo/` | Precise coords when the site gives them; otherwise a per-country gazetteer (ES provinces) |
| Storage | `mtg_engine/models.py` | Cards, listings, matches, deals, events, scan runs |
| API + dashboard | `mtg_engine/api/`, `mtg_engine/web/` | FastAPI; static dark dashboard (Leaflet map, no build step) |
| Agents | `mtg_engine/agents/` | Outbox events, deal state machine, `Agent` base class, stub agents |
| ML training | `ml/` | Dataset builder, multi-task trainer, gallery builder, RunPod launcher |

## 2. Data flow and decisions

1. **Catalog sync**: download every printing in the target sets (plus "confuser"
   sets for vision: Unlimited, Revised, Collectors' Edition) from Scryfall.
2. **Price sync**: Cardmarket price guide (`trend`, then `avg`, then `low`),
   joined on Scryfall's `cardmarket_id`. If that is missing, use Scryfall `eur`,
   then `usd × fx`. The price source is stored per card.
3. **Scan**: each enabled marketplace runs each query. Wallapop is geo-scoped, so
   we sweep a list of search points that together cover Spain (peninsula, Balearics,
   Canaries). Listings are deduplicated by `marketplace:external_id`.
4. **Match**:
   - *Text*: normalised title and description, card names checked on word
     boundaries. Short or ambiguous names ("Fog", "Island") need edition evidence.
     Quantity parsing (`4x`, `x4`, `4 x`). Edition keywords per target set.
   - *Red flags* lower the confidence: proxy, réplica, fake, playtest, gold border,
     Collector's Edition, unlimited/revised.
   - *Ambiguous edition* (card exists in both LEA and LEB, and the ad names both
     or neither): value it at the **cheaper** printing, flagged `edition_ambiguous`.
   - *Vision* (when enabled): detect cards in the ad photos, embed them, look up the
     nearest gallery card, and predict the edition. Merged with the text evidence.
5. **Value and rank**: `total_value = Σ qty × price`, `ratio = total_value / asking`,
   `score = Σ confidence × qty × price`. The dashboard ranks by total value.
6. **Events**: `listing.discovered`, `listing.matched`, `listing.updated`,
   `listing.gone`, `deal.stage_changed`. A matched listing above the threshold
   opens a `Deal` at stage `new`.

## 3. Card recognition model

**Base model**: [`dhvazquez/mtg_card_identification_embeddings`](https://huggingface.co/dhvazquez/mtg_card_identification_embeddings),
EfficientNet-Lite0 + SubCenterArcFace, image → embedding, reported 94.1% Recall@1.
It does retrieval over a gallery, which suits us better than a fixed classifier:
a new set (or game) needs only new gallery images.

Other options looked at:
- `hj3yoo/mtg_card_detector`: YOLOv3-tiny + perceptual hash. Useful idea for detection.
- `fortierq/mtgscan`: OCR + SymSpell. Good fallback for the card name.
- `LauriHursti/visions`: CNN + LSTM OCR. Only works on modern frames, so it does not fit Alpha/Beta.

**Fine-tuning plan** (`ml/`):
1. `build_dataset.py`: Scryfall images for LEA, LEB, 2ED, 3ED, CED and CEI.
2. Multi-task network: shared backbone →
   - an **identity embedding** (SubCenterArcFace over oracle names; Alpha and Beta
     prints of a card share an identity), and
   - an **edition head** (LEA / LEB / 2ED / 3ED / CED / other).
3. Augmentations that look like marketplace photos: perspective, rotation, blur,
   JPEG artefacts, glare, sleeves and toploaders, busy backgrounds, low resolution.
4. Export to ONNX (2 outputs) and build `gallery.npz` from clean scans.
5. Train on RunPod (`ml/runpod/`), one RTX 4090 / A5000-class GPU, about 1–2 h.

**Known limit**: Alpha and Beta share the same artwork. They differ mainly in
corner radius and small print details, so from phone photos the edition head is a
*hint*, not proof. Unlimited and Revised (white border) are much easier to rule
out. Final authentication stays with a human or the negotiation agent (asking for
photos of the corners).

## 4. Agents (downstream workflow)

Deal state machine (`mtg_engine/agents/workflow.py`):

```
new → shortlisted → contacted → negotiating → agreed → paid → shipped → received
  ↘ rejected (from any non-final stage)       ↘ cancelled
```

- The **outbox** table holds domain events. Agents claim them, process them and
  ack them, either in-process (`mtg-engine agents run`) or over HTTP
  (`GET /api/events`, `POST /api/events/{id}/ack`), so an agent can be a separate
  service, such as an LLM agent built on the Claude Agent SDK.
- The `Agent` base class declares `subscribes`, a `handle(event)` method, and the
  deal-stage changes it may make. Each stage change is checked and recorded.
- **Channel interfaces** (`MessagingChannel`, `PaymentChannel`, `ShippingChannel`)
  keep agents separate from the platforms. Wallapop/Milanuncios chat has no public
  API, so the first channel is *draft-only*: a human sends the message.
- Included stubs: `OutreachAgent` (writes a Spanish first-contact message asking
  for corner and back photos), `NegotiationAgent` (offer policy based on value and
  ratio), `LogisticsAgent` (placeholder for Wallapop Envíos / Correos / SEUR).

## 5. Roadmap

### Phase 0: Foundations ✅ (this PR)
- Repo layout, config, DB models, CLI, tests
- MTG catalog (Scryfall) and Cardmarket and Scryfall prices
- Wallapop and Milanuncios clients (HTTP; Playwright fetcher for Milanuncios anti-bot)
- Text matcher, valuation, ranking, Spain gazetteer
- FastAPI and dark dashboard with map
- Outbox, deal state machine, agent base class and stubs
- ML: dataset, training, ONNX export, gallery, RunPod launcher scripts

### Phase 1: Live validation (in progress)
- ✅ Catalog + prices live: 1,809 printings (LEA/LEB + lookalike sets), 1,803 priced from the Cardmarket
  guide; valued at the median of its price fields, since trend alone swings wildly on thin markets
- ✅ Wallapop live: `api.wallapop.com/api/v3/search` with `distance_in_km`; works from a non-EU IP
  (`x-wallapop-request-country-code: ES`); real capture kept as a test fixture
- ✅ Matcher hardened on real ads: SEO keyword stuffing, generic names, other-printing flags,
  placeholder prices ("precio x10"), implausible value/price ratios
- ⛔ Milanuncios: Imperva bot wall ("Pardon Our Interruption") even for a real headless browser from
  a datacenter IP. Options: run the scanner from a Spanish residential connection, a commercial
  proxy that is allowed to scrape it, or an official data agreement with Adevinta. Bot-detection
  evasion is out of scope.
- ✅ Map: bundled province layer, so it renders without tiles; tiles configurable (OSM dark default)
- Schedule hourly scans (`mtg-engine scheduler`)

### Phase 2: Vision
- Train on RunPod and publish the weights (HF Hub or a release asset)
- Turn on `vision.enabled`, evaluate on a hand-labelled set of real ad photos
- Active learning: dashboard "confirm / reject match" → saved labels → retrain

### Phase 3: Scale out
- More marketplaces (Todocoleccion, eBay.es, Vinted, Cardmarket listings)
- More games (Pokémon Base Set / Jungle / Fossil: provider already included)
- Other countries: gazetteer + search points per country
- Postgres, a worker queue (RQ/Celery/Arq), alerts (Telegram/email) for high-ratio deals

### Phase 4: Agents
- LLM outreach and negotiation agents with human approval steps
- Payment escrow (Wallapop protected payment), shipping labels, tracking updates
- Authenticity checklist (corner radius, back, light test) before payment
- Post-purchase: inventory, grading, resale

## 6. Running it

See `README.md`.
