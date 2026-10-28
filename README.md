# deck-price-data (public data repo)

Builds TCGplayer price history for every Magic card from MTGJSON, once a day, and publishes it as small static files
that the Deck Shelf / Deck Price Check app reads. It holds only public price data, so the repo is public.

Setup: create an empty **public** repo named `deck-price-data`, put `build_history.py` and
`price-history.yml` (as `.github/workflows/price-history.yml`) in it, then run the workflow once from the Actions tab.

## Two branches, so the daily download stays small

- **`data`** (recent, force-pushed as one commit every day): only the days of the current 90-day block, so at most 90 days
  and usually fewer. This is the only thing the app re-downloads daily.
- **`archive`** (finished blocks, normal commits): when a 90-day block ends, it is written once to `a/<block>/` and never
  rewritten. The app downloads each block once and keeps it. Blocks are fixed windows counted from 1970-01-01
  (block = day number // 90). Blocks older than 2 years (`--keep-days 730`) are deleted.

MTGJSON only publishes the last 90 days, so history before the first run can't be recovered. The first run archives
whatever part of the previous block MTGJSON still has; from then on the archive grows by one block every 90 days.

## Files

Served from `https://raw.githubusercontent.com/bigpun054/deck-price-data/data/`:
- `meta.json`: `{"latest", "d0", "days", "block", "arch": [block numbers], "keep", "archive", "built", "cards", "shards"}`
  (`d0`/`days` describe the recent files, `archive` is the base URL of the archive branch)
- `s/<first 3 characters of the Scryfall id>.json`: `{"d0", "c": {"<scryfall id>": {"n": [...], "f": [...], "e": [...]}}}`
  with prices in US cents for nonfoil, foil and etched, one value per day starting at day number `d0` (days since 1970-01-01), `null` where there is no price.
- `n/<first 2 letters or digits of the card name>.json`: the card-name index (format 3: format 2 plus card details `d` per name and extras per printing: `e` frame effects, `u` TCGplayer product id, `fn` flavor name, `k` token numbers the card makes), `{"<normalized name>": {"n": "<card name>", "o": "<Scryfall oracle id>", "p": [<printing>, ...]}}`.
  Each printing is `{"i": Scryfall id, "s": SET, "c": collector number, "f": finishes, "t": [special treatments], "r": rarity letter, "$": latest price}`:
  `f` is letters `n` nonfoil, `f` foil, `e` etched, `s` signed (what the printing exists in); `t` holds MTGJSON's promo types and frame effects as written (for example `surgefoil`, `showcase`), a non-black border colour, and `fullart`, `promo`, `oversized`, `textless`, `alternative` (left out when there is none); `r` is `c`/`u`/`r`/`m`/`s`pecial/`b`onus; `$` is the latest available TCGplayer price in US cents per finish (`n`/`f`/`e`), only for finishes that have a price.
  It lists every printing that has price data, so a name like `sol ring` turns into all its printings and Scryfall ids.
- `t/<token set code>.json`: `{"<token number>": {"i": Scryfall id, "n", "ty", "tx", "pw", "to", "co", "ci", "kw", "ly", "u", "f", "$", "fc": faces}}` for every token printing, from MTGJSON tokens.csv. A card's printing lists the numbers in its token set (`sets.json` `ts`) under `k`.
- `n/` card details `d`: `{"ty" type line, "tx" text, "mc" mana cost, "mv" mana value, "co" colors, "ci" color identity, "kw" keywords, "pw"/"to"/"lo"/"de" power/toughness/loyalty/defense, "pm" produced mana, "ly" layout (omitted when normal), "gc" game changer, "nm" full name and "fc" faces for multi-faced cards}`.
- `names.json`: the card-name list for the app's search, one row per card name: `[name, Scryfall oracle id, one printing id for the picture, mana cost, type line, colour identity letters]`. `meta.json` has `namesV`, a stamp that changes only when the list changes, so the app downloads it rarely.
- `sets.json`: `{"<SET>": {"n": set name, "d": release date, "t": set type}}` for the sets in the name index. Join it to a printing's `s` for the set name and release date. The normalized name is lowercase letters and digits with single spaces; the file is picked by its first 2 characters with spaces dropped (`so.json` for `sol ring`). Double-faced cards appear under the full name and under each face name. `meta.json` also has `names` (printings listed) and `nameFiles`.

Served from `https://raw.githubusercontent.com/bigpun054/deck-price-data/archive/`:
- `a/<block>/<first 3 characters of the Scryfall id>.json`: same shape, `d0` = block * 90, 90 values per series.

## Settings

In `price-history.yml`: `--keep-days 730` is how long the archive keeps history (2 years for now; raise it to keep more,
the archive just grows by about one block per 90 days). `--block-days` must not be larger than `--days` (90, MTGJSON's window)
and shouldn't change once an archive exists, because block numbers depend on it.

## Test locally

`python3 build_history.py --prices AllPrices.json.gz --identifiers cardIdentifiers.csv --out out --archive-dir arch --prev prev`
(`--archive-dir` and `--prev` are optional; without them only the recent files are built).
Unit tests for the block logic: `python3 tests/test_build_history.py` in the app repo.
