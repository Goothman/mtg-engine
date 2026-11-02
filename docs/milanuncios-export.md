# Milanuncios export (browser console)

Milanuncios blocks automated access (Imperva "Pardon Our Interruption"), so its ads come in through
an export made in a normal browser, at a person's pace, then imported by the scan workflow.

1. In Chrome/Firefox, open https://www.milanuncios.com/anuncios/?s=cartas+magic (logged in or not).
2. Open the console (F12 → Console). Chrome asks you to type `allow pasting` the first time.
3. Paste the single line from `scripts/milanuncios_export.js` and press Enter.
4. Keep the tab open: it reads every results page (41 ads each) and then every ad page for all
   photos, with 1–3 s pauses: about 15–20 minutes for ~600 ads. Progress shows in green.
   If the site asks to prove you're human, do it in another tab: the script waits and retries.
5. A file `milanuncios_cartas_magic_<date>.json` downloads. Put it in `imports/` and run the
   scan workflow with `marketplace=milanuncios` and `import_file=imports/<file>` (or locally:
   `mtg-engine import-ads imports/<file>`). Known ads are updated; only new photos are read.

The script reads the data the page embeds (`window.__INITIAL_PROPS__`), not the visible links:
a results page shows only a few ad links in its markup but embeds all 41 ads. Results pages
carry only each ad's first photo, so the ad pages are opened for the rest.

To change the script, edit `scripts/milanuncios_export.src.js` and rebuild the one-line version:
`python scripts/build_milanuncios_export.py`.
