# deck-price-data (public data repo)

Builds 90 days of TCGplayer price history for every Magic card from MTGJSON, once a day, and publishes it as small
static files that the Deck Price Check app reads. It holds only public price data, so the repo is public.

Setup: create an empty **public** repo named `deck-price-data`, put `build_history.py` and
`price-history.yml` (as `.github/workflows/price-history.yml`) in it, then run the workflow once from the Actions tab.
The workflow force-pushes a single commit to the `data` branch each day, so the repo stays small.

Files served from `https://raw.githubusercontent.com/bigpun054/deck-price-data/data/`:
- `meta.json`: `{"latest", "d0", "days", "built"}`
- `s/<first 3 characters of the Scryfall id>.json`: `{"d0", "c": {"<scryfall id>": {"n": [...], "f": [...], "e": [...]}}}`
  with prices in US cents for nonfoil, foil and etched, one value per day starting at day number `d0` (days since 1970-01-01), `null` where there is no price.

Test locally: `python3 build_history.py --prices AllPrices.json.gz --identifiers cardIdentifiers.csv --out out`.
