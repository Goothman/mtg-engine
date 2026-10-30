# mtg-engine: working rules

**Goal:** find Magic: The Gathering **Alpha/Beta** cards whose sellers don't know their value:
the needle in the haystack. Generic ads ("cartas magic" lots, collections, bulk) are the target;
ads that already name Alpha/Beta are secondary.

Rules that follow from that (the owner asked for each of these explicitly):

- **Check every photo of every ad.** Never cap the number of photos per ad
  (`vision.max_images_per_listing: null`); a valuable card can be in the last photo of a lot.
- **Never penalise bargains.** A price far below the cards' value is what we're looking for;
  no value/price-ratio demotion. Catch fakes directly instead (proxies, disguised "P r x y",
  sellers who sell proxies, foils, other languages, black-bordered reprints like FBB/IVBB/CE).
- **English Alpha/Beta only.** Other languages, foil, Collectors'/International Edition and
  white-bordered cards (Unlimited, Revised, 4th, 5th...) are never Alpha/Beta.
- **Read every result.** Wallapop: one search point covering all of Spain (1000 km radius) and
  no page limit (`max_pages: null`): "cartas magic" returns 19,000+ ads, all Magic-related as
  deep as sampled. Fixed page limits and per-city points saw under 10% of them.
  The search is read to the end first (text only); photos of new ads are read afterwards
  (`rematch --keep-photos`, the rest of the run), so reading photos never cuts the search short.
- **Generic search first.** Searches are "cartas magic" only (Wallapop and Milanuncios); a paid
  result budget (Apify) goes to that search.
- **Incremental scans.** Known ads: update price/status, reuse stored photo readings, read only
  photos never read. Full photo re-reads only when asked (`recheck`).
- **Check real results before publishing.** Rule changes are replayed on stored data and the
  dashboard diff is inspected (what appears, what disappears and why) before pushing.

Practicalities: the dashboard is the static export in `site/` (Vercel deploys `main`); the scan
runs in GitHub Actions (`.github/workflows/scan.yml`, database on the `engine-data` branch);
the cloud sandbox can't reach marketplace image hosts, so photo work runs in Actions.
