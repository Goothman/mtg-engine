/* MTG Engine dashboard — vanilla JS, no build step. All ad text is untrusted: always esc(). */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const eur = (v, d = 0) => (v == null ? "—" : new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", maximumFractionDigits: d }).format(v));
  const place = (l) => [...new Set([l.city, l.region].filter(Boolean))].join(", ");
  const pct = (v) => `${Math.round(v * 100)}%`;
  const ago = (iso) => {
    if (!iso) return "";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  };
  // Sequential blue ramp (dark surface): brighter = more valuable.
  const RAMP = ["#184f95", "#256abf", "#3987e5", "#6da7ec", "#9ec5f4", "#cde2fb"];

  const state = { config: null, items: [], selected: null, markers: new Map(), map: null, layer: null };

  // Static snapshot mode (e.g. Vercel): data embedded as window.MTG_STATIC, read-only, filtered client-side.
  const STATIC = window.MTG_STATIC || null;
  const ASSETS = STATIC ? "static/" : "/static/";

  function staticApi(path, opts) {
    if (opts && opts.method && opts.method !== "GET") throw new Error("read-only snapshot");
    const u = new URL(path, "http://x");
    const p = u.searchParams;
    if (u.pathname === "/api/config") return STATIC.config;
    if (u.pathname === "/api/stats") return STATIC.stats;
    if (u.pathname === "/api/deals") return STATIC.deals;
    if (u.pathname === "/api/events") return STATIC.events;
    if (u.pathname === "/api/listings") {
      const q = (p.get("q") || "").toLowerCase();
      const minV = +p.get("min_value") || 0, minR = +p.get("min_ratio") || 0;
      const minP = p.get("min_price"), maxP = p.get("max_price");
      let items = STATIC.listings.filter((l) =>
        l.match_count > 0 &&
        (!p.get("source") || l.photo_scan === (p.get("source") === "generic")) &&
        (minP == null || (l.price_eur ?? 0) >= +minP) && (maxP == null || (l.price_eur ?? 0) <= +maxP) &&
        (!p.get("marketplace") || l.marketplace === p.get("marketplace")) &&
        (!p.get("edition") || (l.editions || []).includes(p.get("edition"))) &&
        (!p.get("found") || (p.get("found") === "photo") === isPhotoFind(l)) &&
        l.est_total_value >= minV && (!minR || (l.value_ratio || 0) >= minR) &&
        (!q || [l.title, l.description, l.city].join(" ").toLowerCase().includes(q)));
      const key = { value: (l) => -l.est_total_value, ratio: (l) => -(l.value_ratio || 0), score: (l) => -l.est_score,
        newest: (l) => -new Date(l.first_seen_at).getTime() }[p.get("sort") || "value"];
      items = items.slice().sort((a, b) => key(a) - key(b));
      return { total: items.length, items: items.slice(0, 500) };
    }
    throw new Error(`not in snapshot: ${u.pathname}`);
  }

  async function api(path, opts = {}) {
    if (STATIC) return staticApi(path, opts);
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!res.ok) throw new Error(`${res.status} ${(await res.text()).slice(0, 200)}`);
    return res.json();
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 3000);
  }

  // ---------------------------------------------------------------- KPIs
  async function loadStats() {
    const s = await api("/api/stats");
    const tiles = [
      ["Matched ads", s.listings_matched, `${s.listings_active} active ads tracked`],
      ["Total est. value", eur(s.total_value_eur), "sum over matched ads"],
      ["Total asking", eur(s.total_asking_eur), "what sellers ask"],
      ["Best value / price", s.best_ratio ? `${s.best_ratio.toFixed(2)}×` : "—", "highest single ad"],
      ["From “cartas magic”", s.matched_generic ?? 0, `${s.photo_finds ?? 0} spotted only in photos`],
      ["Open deals", Object.entries(s.deals_by_stage).filter(([k]) => !["received", "rejected", "cancelled"].includes(k)).reduce((a, [, v]) => a + v, 0), `${s.cards_in_catalog} cards in catalog`],
    ];
    $("#kpis").innerHTML = tiles.map(([l, v, h]) => `<div class="kpi"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div><div class="hint">${esc(h)}</div></div>`).join("");
    const ls = s.last_scan;
    $("#last-scan").textContent = ls ? `Last scan ${ago(ls.finished_at || ls.started_at)}${ls.finished_at ? "" : " (running)"} · ${ls.stats?.fetched ?? 0} ads fetched${ls.errors?.length ? ` · ${ls.errors.length} errors` : ""}` : "No scans yet";
    if (ls?.errors?.length) $("#last-scan").title = ls.errors.join("\n");
  }

  // ---------------------------------------------------------------- filters
  function filterParams() {
    const f = new FormData($("#filters"));
    const p = new URLSearchParams();
    for (const [k, v] of f.entries()) if (v !== "") p.set(k, v);
    if (p.get("source") === "photo") { p.set("source", "generic"); p.set("found", "photo"); }
    return p;
  }

  // Asking-price slider: two overlapping range inputs on a log scale (cards go from 1 € to 50.000 €).
  const PRICE_STEPS = 1000;
  function niceRound(v) {
    const step = v < 100 ? 1 : v < 1000 ? 10 : v < 10000 ? 50 : 500;
    return Math.round(v / step) * step;
  }
  function posToPrice(pos) {
    return pos <= 0 ? 0 : niceRound(Math.expm1((pos / PRICE_STEPS) * Math.log1p(state.priceMax)));
  }
  function initPriceRange(maxPrice) {
    state.priceMax = Math.max(niceRound(maxPrice || 0) || 0, 100);
    const lo = $("#price-lo"), hi = $("#price-hi");
    const sync = (moved) => {
      if (+lo.value > +hi.value) (moved === lo ? hi : lo).value = moved.value;
      const a = +lo.value, b = +hi.value;
      $("#filters [name=min_price]").value = a > 0 ? posToPrice(a) : "";
      $("#filters [name=max_price]").value = b < PRICE_STEPS ? posToPrice(b) : "";
      $("#price-label").textContent = a <= 0 && b >= PRICE_STEPS ? "any"
        : `${a > 0 ? eur(posToPrice(a)) : "0 €"} – ${b < PRICE_STEPS ? eur(posToPrice(b)) : `${eur(state.priceMax)}+`}`;
      $(".range-track").style.setProperty("--lo", `${(a / PRICE_STEPS) * 100}%`);
      $(".range-track").style.setProperty("--hi", `${(b / PRICE_STEPS) * 100}%`);
    };
    lo.addEventListener("input", () => sync(lo));
    hi.addEventListener("input", () => sync(hi));
    sync(lo);
  }

  // ---------------------------------------------------------------- listings
  function valueColor(v, max) {
    if (!v) return RAMP[0];
    const t = Math.log1p(v) / Math.log1p(Math.max(max, 1));
    return RAMP[Math.min(RAMP.length - 1, Math.floor(t * RAMP.length))];
  }

  const SOFT_FLAGS = new Set(["photo_match", "photo_unavailable"]);

  function isPhotoFind(l) {
    return (l.flags || []).includes("photo_match");
  }

  function sellerName(l) {
    const s = l.seller || {};
    return s.name || l.seller_name || "";
  }

  function sellerHTML(l) {
    const s = l.seller || {};
    const name = sellerName(l);
    if (!name) return "";
    const link = s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(name)}</a>` : esc(name);
    const bits = [];
    if (s.rating != null) bits.push(`★ ${(+s.rating).toFixed(1)}${s.reviews != null ? ` (${s.reviews} reviews)` : ""}`);
    if (s.sold != null) bits.push(`${s.sold} sold`);
    if (s.on_sale != null) bits.push(`${s.on_sale} on sale`);
    if (s.since) bits.push(`since ${esc(s.since.slice(0, 4))}`);
    if (s.verified) bits.push("verified");
    if (s.type && s.type !== "Private") bits.push(esc(s.type));
    return `<span class="small">Seller: <b>${link}</b>${bits.length ? ` <span class="muted">· ${bits.join(" · ")}</span>` : ""}</span>`;
  }

  function rowHTML(l, i) {
    const img = l.images?.[0] ? `<img class="thumb" loading="lazy" src="${esc(l.images[0])}" alt="">` : `<div class="thumb"></div>`;
    const editions = (l.editions || []).map((e) => `<span class="chip edition">${esc(labelFor(e))}</span>`).join("");
    const flags = (l.flags || []).filter((f) => !SOFT_FLAGS.has(f)).map((f) => `<span class="chip flag" title="red flag">⚠ ${esc(f)}</span>`).join("");
    const photo = isPhotoFind(l) ? `<span class="chip photo" title="Card recognised in the ad photos; the text doesn't name it">📷 found in photos</span>` : "";
    const review = l.needs_review ? `<span class="chip review">needs review</span>` : "";
    const demo = l.is_demo ? `<span class="chip demo">demo data</span>` : "";
    const deal = l.deal ? `<span class="chip">deal: ${esc(l.deal.stage)}</span>` : "";
    const ratio = l.value_ratio != null ? `<div class="ratio ${l.value_ratio >= 1 ? "good" : "bad"}">${l.value_ratio.toFixed(2)}× asking</div>` : "";
    return `
      <div class="row" data-id="${esc(l.id)}">
        <div class="rank">${i + 1}</div>
        ${img}
        <div style="min-width:0">
          <div class="title" title="${esc(l.title)}">${esc(l.title)}</div>
          <div class="meta"><span>${esc(l.marketplace)}</span><span>${esc(place(l) || "location unknown")}</span>${sellerName(l) ? `<span title="seller">👤 ${esc(sellerName(l))}</span>` : ""}<span>${l.match_count} card${l.match_count === 1 ? "" : "s"} matched</span><span>${esc(ago(l.first_seen_at))}</span></div>
          <div class="chips">${editions}${photo}${review}${flags}${deal}${demo}</div>
        </div>
        <div class="money">
          <div class="value">${eur(l.est_total_value)}</div>
          <div class="ask">asking ${eur(l.price_eur)}</div>
          ${ratio}
        </div>
      </div>`;
  }

  function detailHTML(l) {
    const rows = (l.matches || []).map((m) => `
      <tr class="${m.status === "rejected" ? "rejected" : ""}">
        <td>${m.image_url ? `<img class="card-img" src="${esc(m.image_url)}" alt="">` : ""}${esc(m.name)}</td>
        <td>${esc(labelFor(m.set_code))}${m.edition_ambiguous ? ' <span class="muted" title="Edition not certain; valued at the cheaper printing">(?)</span>' : ""}</td>
        <td class="r">${m.quantity}</td>
        <td class="r">${eur(m.unit_value_eur, 2)}</td>
        <td class="r">${eur(m.line_value_eur, 2)}</td>
        <td class="r" title="${esc(m.source)}${m.matched_text ? " · " + esc(m.matched_text) : ""}">${m.source.includes("vision") ? '<span title="recognised in photo">📷 </span>' : ""}${pct(m.confidence)}${m.status === "confirmed" ? " ✓" : ""}</td>
        <td class="r">
          <button class="btn sm ghost" data-act="confirm" data-mid="${m.id}" title="Confirm match">✓</button>
          <button class="btn sm ghost" data-act="reject" data-mid="${m.id}" title="Reject match">✕</button>
        </td>
      </tr>`).join("");
    const gallery = (l.images || []).slice(0, 8).map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(u)}" alt=""></a>`).join("");
    const dealBtn = l.deal ? `<span class="muted small">Deal #${l.deal.id} · stage <b>${esc(l.deal.stage)}</b></span>` : `<button class="btn sm" data-act="deal">Open deal</button>`;
    return `
      <div class="detail">
        ${gallery ? `<div class="gallery">${gallery}</div>` : ""}
        <div class="desc">${esc(l.description || "(no description)")}</div>
        ${rows ? `<table class="cards"><thead><tr><th>Card</th><th>Edition</th><th class="r">Qty</th><th class="r">Unit</th><th class="r">Line</th><th class="r">Conf.</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="muted small">No individual cards identified. Check the photos.</div>`}
        <div class="detail-actions">
          <a class="btn sm" href="${esc(l.url)}" target="_blank" rel="noopener">Open ad ↗</a>
          ${dealBtn}
          ${sellerHTML(l)}
          <span class="muted small">Prices: ${esc([...new Set((l.matches || []).map((m) => m.price_source).filter(Boolean))].join(", ") || "—")}</span>
        </div>
      </div>`;
  }

  function labelFor(code) {
    for (const g of Object.values(state.config?.games || {})) for (const t of g.targets) if (t.set_code === code) return t.label;
    return (code || "").toUpperCase();
  }

  async function loadListings() {
    const data = await api(`/api/listings?${filterParams()}`);
    state.items = data.items;
    $("#result-count").textContent = `${data.total} ad${data.total === 1 ? "" : "s"}`;
    const box = $("#listings");
    box.innerHTML = data.items.length ? data.items.map(rowHTML).join("") : `<div class="empty">No findings yet. Run a scan, or loosen the filters.</div>`;
    if (state.selected) expand(state.selected, false);
    renderMap();
  }

  function expand(id, scroll = true) {
    document.querySelectorAll(".row.selected").forEach((r) => { r.classList.remove("selected"); r.querySelector(".detail")?.remove(); });
    const row = document.querySelector(`.row[data-id="${CSS.escape(id)}"]`);
    const l = state.items.find((x) => x.id === id);
    if (!row || !l) return;
    state.selected = id;
    row.classList.add("selected");
    row.insertAdjacentHTML("beforeend", detailHTML(l));
    if (scroll) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    const mk = state.markers.get(id);
    if (mk) mk.openPopup();
  }

  // ---------------------------------------------------------------- map
  const TILES = {
    osm_dark: { url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", cls: "tiles-dark",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' },
    osm: { url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' },
    carto_dark: { url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", subdomains: "abcd",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>' },
  };

  function initMap() {
    if (!window.L) { $("#map").innerHTML = '<div class="empty">Map library failed to load</div>'; return; }
    const c = state.config;
    const mc = c.map || {};
    state.map = L.map("map", { zoomControl: true, preferCanvas: true }).setView(c.map_center, c.map_zoom);
    const t = TILES[mc.tiles];
    if (t) {
      L.tileLayer(t.url, { attribution: t.attribution, subdomains: t.subdomains || "abc", maxZoom: 19, className: t.cls || "" }).addTo(state.map);
    }
    // Bundled province shapes: the map stays readable even if the tile server is unreachable.
    if (mc.boundaries) {
      fetch(`${ASSETS}${mc.boundaries}`).then((r) => r.json()).then((geo) => {
        L.geoJSON(geo, {
          interactive: false,
          style: { color: "#55544f", weight: 1, fillColor: "#2c2c2a", fillOpacity: t ? 0.05 : 0.9 },
        }).addTo(state.map).bringToBack();
        state.map.attributionControl.addAttribution("Boundaries &copy; IGN (es-atlas)");
      }).catch(() => {});
    }
    state.layer = L.layerGroup().addTo(state.map);
  }

  function renderMap() {
    if (!state.map) return;
    state.layer.clearLayers();
    state.markers.clear();
    const pts = state.items.filter((l) => l.lat != null);
    const max = Math.max(1, ...pts.map((l) => l.est_total_value || 0));
    for (const l of pts.slice().sort((a, b) => a.est_total_value - b.est_total_value)) {
      const exact = l.geo_precision === "exact";
      const r = 6 + 14 * Math.sqrt((l.est_total_value || 0) / max);
      const color = valueColor(l.est_total_value, max);
      const m = L.circleMarker([l.lat, l.lon], {
        radius: r, color: exact ? "#1a1a19" : color, weight: 2, dashArray: exact ? null : "3 3",
        fillColor: color, fillOpacity: exact ? 0.85 : 0.45,
      });
      m.bindPopup(`<b>${esc(l.title)}</b><br>${esc(l.marketplace)} · ${esc(place(l))}${exact ? "" : " <span class='muted'>(approx.)</span>"}${sellerName(l) ? ` · 👤 ${esc(sellerName(l))}` : ""}<br>Value <b>${eur(l.est_total_value)}</b> · asking ${eur(l.price_eur)}${l.value_ratio ? ` · ${l.value_ratio.toFixed(2)}×` : ""}<br><a href="${esc(l.url)}" target="_blank" rel="noopener">Open ad ↗</a>`);
      m.bindTooltip(`${eur(l.est_total_value)} · ${esc(l.title.slice(0, 40))}`, { direction: "top" });
      m.on("click", () => expand(l.id));
      m.addTo(state.layer);
      state.markers.set(l.id, m);
    }
  }

  // ---------------------------------------------------------------- deals
  async function loadDeals() {
    const deals = await api("/api/deals");
    const by = Object.fromEntries(state.config.stages.map((s) => [s, []]));
    deals.forEach((d) => (by[d.stage] ||= []).push(d));
    $("#deals").innerHTML = state.config.stages.map((stage) => `
      <div class="col"><h3><span>${esc(stage)}</span><span>${by[stage].length}</span></h3>
        ${by[stage].map((d) => `
          <div class="deal">
            <div class="t" title="${esc(d.listing.title)}">${esc(d.listing.title)}</div>
            <div class="muted small">${eur(d.listing.est_total_value)} value · ${eur(d.listing.price_eur)} asking${d.offer_price_eur ? ` · offer ${eur(d.offer_price_eur)}` : ""}</div>
            <div class="moves">${d.allowed_next.map((n) => `<button class="btn sm" data-deal="${d.id}" data-to="${esc(n)}">→ ${esc(n)}</button>`).join("")}<a class="btn sm ghost" href="${esc(d.listing.url)}" target="_blank" rel="noopener">ad ↗</a></div>
            ${d.drafts?.length ? `<details><summary>${d.drafts.length} message draft(s)</summary>${d.drafts.map((x) => `<pre>${esc(x.text)}</pre>`).join("")}</details>` : ""}
            ${d.notes?.length ? `<details><summary>${d.notes.length} note(s)</summary>${d.notes.map((n) => `<div>${esc(n.by)}: ${esc(n.text)}</div>`).join("")}</details>` : ""}
          </div>`).join("")}
      </div>`).join("");
  }

  async function loadEvents() {
    const evs = await api("/api/events?limit=100");
    $("#events tbody").innerHTML = evs.map((e) => `<tr><td>${e.id}</td><td>${esc(e.type)}</td><td><code>${esc(e.subject_id)}</code></td><td><code>${esc(JSON.stringify(e.payload))}</code></td><td>${esc(ago(e.created_at))}</td><td>${esc(e.claimed_by || "")}</td></tr>`).join("") || `<tr><td colspan="6" class="empty">No events yet</td></tr>`;
  }

  // ---------------------------------------------------------------- wiring
  async function refresh() {
    try {
      await Promise.all([loadStats(), loadListings()]);
      const view = document.querySelector(".tab.active").dataset.view;
      if (view === "deals") await loadDeals();
      if (view === "events") await loadEvents();
    } catch (e) { toast(`Error: ${e.message}`); }
  }

  async function init() {
    state.config = await api("/api/config");
    const mk = $("#filters [name=marketplace]");
    state.config.marketplaces.forEach((m) => mk.insertAdjacentHTML("beforeend", `<option value="${esc(m)}">${esc(m)}</option>`));
    const ed = $("#filters [name=edition]");
    Object.values(state.config.games).forEach((g) => g.targets.forEach((t) => ed.insertAdjacentHTML("beforeend", `<option value="${esc(t.set_code)}">${esc(t.label)}</option>`)));
    $("#subtitle").textContent = `${Object.values(state.config.games).flatMap((g) => g.targets.map((t) => t.label)).join(" · ")} · ${state.config.marketplaces.join(", ")} · ${state.config.country}`;
    initMap();
    try { initPriceRange((await api("/api/stats")).max_price_eur); } catch { initPriceRange(0); }

    let deb;
    $("#filters").addEventListener("input", () => { clearTimeout(deb); deb = setTimeout(loadListings, 250); });
    $("#filters").addEventListener("submit", (e) => e.preventDefault());

    $("#listings").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-act]");
      const row = e.target.closest(".row");
      if (btn) {
        e.stopPropagation();
        try {
          if (btn.dataset.act === "deal") { await api(`/api/listings/${encodeURIComponent(row.dataset.id)}/deal`, { method: "POST" }); toast("Deal opened"); }
          else { await api(`/api/matches/${btn.dataset.mid}`, { method: "POST", body: JSON.stringify({ status: btn.dataset.act === "confirm" ? "confirmed" : "rejected" }) }); }
          await refresh();
        } catch (err) { toast(`Error: ${err.message}`); }
        return;
      }
      if (e.target.closest(".detail") || e.target.closest("a")) return;
      if (row) row.classList.contains("selected") ? (row.classList.remove("selected"), row.querySelector(".detail")?.remove(), (state.selected = null)) : expand(row.dataset.id);
    });

    $("#deals").addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-deal]");
      if (!b) return;
      try { await api(`/api/deals/${b.dataset.deal}/transition`, { method: "POST", body: JSON.stringify({ to: b.dataset.to, by: "human" }) }); await loadDeals(); await loadStats(); }
      catch (err) { toast(`Error: ${err.message}`); }
    });

    document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
      document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${t.dataset.view}`));
      if (t.dataset.view === "findings" && state.map) setTimeout(() => state.map.invalidateSize(), 50);
      refresh();
    }));

    if (STATIC) {
      document.body.classList.add("static-snapshot");
      $("#scan-btn").hidden = true;
      $("#subtitle").textContent += ` · snapshot ${new Date(STATIC.generated_at).toLocaleString("es-ES")} (read-only)`;
    }
    $("#scan-btn").addEventListener("click", async () => {
      const b = $("#scan-btn");
      b.disabled = true;
      try { await api("/api/scan", { method: "POST", body: "{}" }); toast("Scan started. Results appear as they arrive."); }
      catch (err) { toast(`Scan: ${err.message}`); }
      setTimeout(() => (b.disabled = false), 5000);
    });

    await refresh();
    setInterval(refresh, 30000);
  }

  init().catch((e) => toast(`Failed to load: ${e.message}`));
})();
