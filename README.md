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
  It lists every paper printing (not MTGO / Arena-only ones), priced or not, plus any other printing that has a price, so a name like `sol ring` turns into all its printings and Scryfall ids. A printing with no TCGplayer price has no `$`; the app then asks Scryfall for that same printing's price. `meta.json` `unpriced` counts the printings listed without a price.
- `t/<token set code>.json`: `{"<token number>": {"i": Scryfall id, "n", "ty", "tx", "pw", "to", "co", "ci", "kw", "ly", "u", "f", "$", "fc": faces}}` for every token printing, from MTGJSON tokens.csv. A card's printing lists the numbers in its token set (`sets.json` `ts`) under `k`.
- `n/` card details `d`: `{"ty" type line, "tx" text, "mc" mana cost, "mv" mana value, "co" colors, "ci" color identity, "kw" keywords, "pw"/"to"/"lo"/"de" power/toughness/loyalty/defense, "pm" produced mana, "ly" layout (omitted when normal), "gc" game changer, "bn" banned in Commander (from MTGJSON cardLegalities.csv), "rk" EDHREC rank (1 = most played; left out when EDHREC has none), "nm" full name and "fc" faces for multi-faced cards}`.
- `w/<same 2-character prefix as n/>.json`: budget swaps for the app's Statistics tab, `{"<normalized name>": [[swap name, cheapest price in cents, mana value, colour identity letters, "p"|"g", EDHREC rank or 0], ...]}`.
  Built for every card whose cheapest printing costs 50 cents or more and that does a job (ramp, draw, removal, board wipe, protection, recursion, win condition, tutor, counterspell; worked out from the card text with the same rules the app uses). Each card gets up to 8 cheaper cards that do all the same jobs, cost half its price or less, fit inside its colour identity and aren't banned in Commander, best match first (text, keywords and card type). `p` = popular pick (EDHREC top 10,000), `g` = hidden gem (a close match outside that list); the list keeps both kinds. The app applies the player's own limits (minimum price, price share, extra mana) and the deck's colours. `meta.json` has `swaps` (cards with swaps) and `swapFiles`.
- `names.json`: the card-name list for the app's search, one row per card name: `[name, Scryfall oracle id, one printing id for the picture, mana cost, type line, colour identity letters]`. `meta.json` has `namesV`, a stamp that changes only when the list changes, so the app downloads it rarely.
- `sets.json`: `{"<SET>": {"n": set name, "d": release date, "t": set type}}` for the sets in the name index. Join it to a printing's `s` for the set name and release date. The normalized name is lowercase letters and digits with single spaces; the file is picked by its first 2 characters with spaces dropped (`so.json` for `sol ring`). Double-faced cards appear under the full name and under each face name. `meta.json` also has `names` (printings listed) and `nameFiles`.

- `combo/`: combo data from [Commander Spellbook](https://commanderspellbook.com/) (built by `build_combos.py`, see its docstring for every field).
  Spellbook's daily export (`variants.json.gz`, about 118,000 combos) is downloaded once a day (skipped when its ETag or timestamp hasn't changed;
  yesterday's files are kept if anything fails) and stored whole, with each card, template, feature and recipe written once:
  - `combo/meta.json`: export timestamp and version, counts, `index` (a hash per index file), `names` (hash of names.json), `formats`.
  - `combo/i/<same 2-character prefix as n/>.json`: per normalized card name, its Spellbook card id `c`, the compact record of every combo it is in
    `v` (`[id, piece card ids, [[template id, count]], feature ids, mana value needed, bracket tag, colour identity, must-be-commander piece indexes]`)
    and the templates it fits `t`. The app downloads only the files for cards the player owns, and again only when a file's hash changes.
  - `combo/v/<first 3 hex of sha1(combo id)>.json`: every combo with all of Spellbook's fields (steps, prerequisites, zones, prices, legality...),
    read when a combo is opened.
  - `combo/names.json`, `cards.json`, `templates.json`, `features.json`, `recipes.json`, `aliases.json`, `matches.json` (template search results).
  Template slots ("any free sac outlet"): a template's Scryfall query is run on Scryfall's search API, a listed-cards template is read from
  Spellbook's `/cards/?matched_by=`; results are reused for 7 days (`--match-days`). Combo data from Commander Spellbook, credited in the app.

Served from `https://raw.githubusercontent.com/bigpun054/deck-price-data/archive/`:
- `a/<block>/<first 3 characters of the Scryfall id>.json`: same shape, `d0` = block * 90, 90 values per series.

## Downloads

The workflow reads these MTGJSON files each day: `AllPrices.json.gz` (prices), `cardIdentifiers.csv` / `AllIdentifiers.json.gz` and `tokenIdentifiers.csv` (Scryfall ids),
`cards.csv` (names, text and EDHREC rank), `tokens.csv`, `sets.csv`, and `cardLegalities.csv` (`--legalities`, for the Commander ban flag).
The names, tokens, sets and legalities downloads are retried and may fail without stopping the prices from being published.
The combo step reads `https://json.commanderspellbook.com/variants.json.gz` plus Scryfall and Spellbook searches for template slots; it can also fail without stopping the prices.

## Settings

In `price-history.yml`: `--keep-days 730` is how long the archive keeps history (2 years for now; raise it to keep more,
the archive just grows by about one block per 90 days). `--block-days` must not be larger than `--days` (90, MTGJSON's window)
and shouldn't change once an archive exists, because block numbers depend on it.

## Test locally

`python3 build_history.py --prices AllPrices.json.gz --identifiers cardIdentifiers.csv --out out --archive-dir arch --prev prev`
(`--archive-dir` and `--prev` are optional; without them only the recent files are built).
Unit tests for the block logic: `python3 tests/test_build_history.py` in the app repo; the ban flag, EDHREC rank and swap files: `python3 tests/test_build_swaps.py`.
