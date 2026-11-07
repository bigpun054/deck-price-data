#!/usr/bin/env python3
"""Build compact TCGplayer price-history shards from MTGJSON, keyed by Scryfall id.

Two kinds of output, so the app only re-downloads a small amount each day:

  recent  (rebuilt every day, served from the `data` branch)
    meta.json     {"latest", "d0", "days", "block", "arch": [block numbers], "keep", "archive", "built", "cards", "shards"}
    s/<first 3 hex of scryfall id>.json
                  {"d0": <day number of index 0>, "c": {"<scryfall id>": {"n": [cents|null,...], "f": [...], "e": [...]}}}
    Holds only the days of the CURRENT block (1 to --block-days days), so it stays small.

  archive (written once per block, never rewritten, served from the `archive` branch)
    a/<block number>/<first 3 hex>.json     same shard format, d0 = block number * block-days, block-days values each
    A block is closed when the calendar rolls into the next one. Blocks are fixed 90-day windows counted from 1970-01-01
    (block = day number // 90), so a closed block's files never change and the app can keep them forever.
    Blocks older than --keep-days are deleted.

  names (optional, rebuilt every day, in the same `data` branch; needs --names, format 2)
    n/<first 2 letters/digits of the name, "_" if none>.json
                  {"<normalized name>": {"n": "<card name>", "o": "<scryfall oracle id>", "p": [<printing>, ...]}}
      printing  = {"i": scryfall id, "s": SET, "c": collector number,
                   "f": finishes this printing exists in, letters n=nonfoil f=foil e=etched s=signed,
                   "t": [special treatments: MTGJSON promoTypes and frameEffects as written (e.g. surgefoil, showcase),
                         a non-black border colour, and fullart / promo / oversized / textless / alternative],  (omitted if none)
                   "r": rarity letter c/u/r/m/s(pecial)/b(onus),
                   "lg": Scryfall language code (ja, zhs, de ...) for a printing that is not in English (omitted for English),
                   "$": {"n"|"f"|"e": latest TCGplayer price in US cents}}  (finishes with a price only)
    Lists every paper printing (and any other printing that has price data), so a card name can be turned into all its printings
    and ids; a printing with no price simply has no "$".
    sets.json     (needs --sets) {"<SET>": {"n": set name, "d": release date, "t": set type}} for the sets used above.
    The normalized name is lowercase letters and digits with single spaces ("sol ring"); the first 2 letters/digits
    of it (spaces dropped) pick the file. Double-faced cards are listed under the full name and under each face name.

  cards (inside the same name files, format 3)
    each name entry also carries "d", the card's details (rules text, type, mana cost, colours ...), and each printing may carry
    "e" (frame effects), "u" (TCGplayer product id), "fn" (flavour name) and "k" (numbers of the tokens it makes, see below).
      d = {"ty": type line, "tx": rules text, "mc": mana cost, "mv": mana value, "co": [colours], "ci": [colour identity],
           "kw": [keywords], "pw": power, "to": toughness, "lo": loyalty, "de": defense, "pm": [mana it can produce],
           "ly": layout (left out for "normal"), "gc": 1 for a Game Changer, "bn": 1 if banned in Commander (needs --legalities;
           meta.json "legalOk" is true when today's ban list was read, false when yesterday's marks were kept or there were none),
           "rk": EDHREC rank, 1 = most played (left out when EDHREC has none)}
      a card with several faces carries "nm" (the full name) and "fc" = [{"n": face name, "mc", "mv", "ty", "tx", "pw", "to", "lo", "de", "co", "pm"}, ...]
      instead of the per-face fields, and keeps "mv", "ci", "kw", "ly", "gc" for the whole card.
  budget swaps (built with the names)
    w/<same 2-letter prefix as n/>.json
                  {"<normalized name>": [[swap name, cheapest price in cents, mana value, colour identity "GW", "p"|"g", EDHREC rank|0], ...]}
      For each card whose cheapest printing costs 50 cents or more and that does a job (ramp, draw, removal, board wipe, protection,
      recursion, win condition, tutor, counterspell): up to 8 cards that do the same jobs, cost half as much or less and fit inside its
      colour identity, best match first. "p" = popular pick (EDHREC top 10,000), "g" = hidden gem (a close match outside that list).
      The app picks from these with the player's own price and mana limits.
  card roles (needs --roles; Build 287, refreshed weekly)
    roles.json    {"at": when Scryfall was last asked, "ok": every role answered, "tags": {"<tag>": total_cards or null},
                   "c": {"<oracle id>": "<role letters>"}}
    Scryfall's community oracle tags (Tagger), asked once a week: r ramp, v removal, a card advantage, d draw, w board wipe, t tutor,
    c counterspell, p protection, e recursion. A card's letters are also written into its details in the name index as "rl"
    (d.rl), so the app reads them with the card. A tag Scryfall doesn't know or answers with a suspicious size keeps last
    week's letters for that role; between refreshes (and when Scryfall can't be reached) the previous roles.json is carried forward.
    meta.json "rolesAt" is when the letters were fetched, "rolesOk" whether every role was answered then.
  tokens (needs --tokens)
    t/<token set code>.json   {"<token number>": {"i": scryfall id, "n": name, "ty", "tx", "pw", "to", "co", "kw", "ly", "u", "f", "$", "fc"}}
    A printing's "k" lists the numbers of the tokens (in its set's token set, from sets.json "ts") that its card makes.

Day number = whole days since 1970-01-01 (UTC). n = nonfoil, f = foil, e = etched. Prices are US cents (TCGplayer retail).
MTGJSON only publishes the last 90 days, so an archive block is assembled from the previous run's recent files (--prev)
plus MTGJSON's window; days neither source has stay null.
"""
import argparse, csv, hashlib, datetime as dt, gzip, io, json, os, re, shutil, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from array import array

try:
    import ijson  # streams the multi-hundred-MB MTGJSON files without loading them whole
except ImportError:  # only for small local test files; the workflow always installs ijson
    ijson = None

FIN = {"normal": "n", "foil": "f", "etched": "e"}
# cards.csv yes/no columns that count as a special treatment, and the tag each one adds
FLAGS = {"isFullArt": "fullart", "isPromo": "promo", "isOversized": "oversized", "isTextless": "textless",
         "isAlternative": "alternative"}


def kvitems(f, prefix, **kw):
    if ijson:
        return ijson.kvitems(f, prefix, **kw)
    return json.load(f)[prefix].items()


def open_any(src):
    """Open a local path or an http(s) URL (downloaded to a temp file first). .gz is decompressed."""
    path = src
    if src.startswith(("http://", "https://")):
        suffix = os.path.basename(src.split("?")[0])
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="-" + suffix)
        req = urllib.request.Request(src, headers={"User-Agent": "deck-price-data/1.0"})
        with urllib.request.urlopen(req, timeout=600) as r:
            got = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                tmp.write(chunk)
                got += len(chunk)
            want = r.headers.get("Content-Length")
            if want and want.isdigit() and got != int(want):   # the connection closed early; never build from half a file
                tmp.close()
                os.unlink(tmp.name)
                raise OSError(f"download cut off: got {got} of {want} bytes from {src}")
        tmp.close()
        path = tmp.name
    return gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")



def with_retries(label, fn, retries, wait):
    """Run fn(); if it fails (a download cut off, a server hiccup), wait `wait` seconds and try again, up to `retries` more times.
    The last failure is raised, so callers keep their own "skip this step" handling."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= retries:
                raise
            print(f"{label}: attempt {attempt + 1} of {retries + 1} failed ({e!r}); trying again in {wait} s", file=sys.stderr, flush=True)
            time.sleep(wait)


def split_list(s):
    """"R, U" -> ["R", "U"]"""
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def num(s):
    """"3.0" -> 3, "0.5" -> 0.5, "" -> None"""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v


def load_ids(src, oracle=None, tcg=None):
    """MTGJSON uuid -> Scryfall id. Accepts cardIdentifiers.csv(.gz) or AllIdentifiers.json(.gz).
    If an `oracle` dict is given, it also receives uuid -> Scryfall oracle id (one id shared by all printings of a card).
    If a `tcg` dict is given, it receives uuid -> TCGplayer product id."""
    f = open_any(src)
    ids = {}
    if src.split("?")[0].endswith((".csv", ".csv.gz")):
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline="")):
            u, s = row.get("uuid"), row.get("scryfallId")
            if u and s:
                ids[u] = s
                if oracle is not None and row.get("scryfallOracleId"):
                    oracle[u] = row["scryfallOracleId"]
                if tcg is not None and row.get("tcgplayerProductId"):
                    tcg[u] = row["tcgplayerProductId"]
    else:
        for u, card in kvitems(f, "data"):
            ident = card.get("identifiers") or {}
            s = ident.get("scryfallId")
            if s:
                ids[u] = s
                if oracle is not None and ident.get("scryfallOracleId"):
                    oracle[u] = ident["scryfallOracleId"]
                if tcg is not None and ident.get("tcgplayerProductId"):
                    tcg[u] = ident["tcgplayerProductId"]
    f.close()
    return ids


def load_names(src):
    """MTGJSON uuid -> (name, set code, collector number, face name). Needs a cards.csv-style file with those columns."""
    csv.field_size_limit(1 << 30)  # cards.csv has some very long text fields
    f = open_any(src)
    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline=""))
    need = {"uuid", "name", "setCode", "number"}
    missing = need - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"missing columns {sorted(missing)}")
    out = {}
    for row in reader:
        u = row.get("uuid")
        if u and row.get("name"):
            out[u] = {
                "name": row["name"], "set": row.get("setCode") or "", "num": row.get("number") or "",
                "face": row.get("faceName") or "", "fin": row.get("finishes") or "",
                "promo": row.get("promoTypes") or "", "frame": row.get("frameEffects") or "",
                "border": row.get("borderColor") or "", "rar": row.get("rarity") or "",
                "flags": [t for col, t in FLAGS.items() if row.get(col) == "true"],
                "side": row.get("side") or "", "layout": row.get("layout") or "", "text": row.get("text") or "",
                "type": row.get("type") or "", "mc": row.get("manaCost") or "", "mv": row.get("manaValue") or "",
                "fmv": row.get("faceManaValue") or "", "colors": row.get("colors") or "", "ci": row.get("colorIdentity") or "",
                "kw": row.get("keywords") or "", "pw": row.get("power") or "", "to": row.get("toughness") or "",
                "lo": row.get("loyalty") or "", "de": row.get("defense") or "", "pm": row.get("producedMana") or "",
                "gc": row.get("isGameChanger") == "true", "flavor": row.get("flavorName") or "",
                "rk": row.get("edhrecRank") or "",
                "rel": row.get("relatedCards") or "",
                "lang": row.get("language") or "",
                # a printing you can own on paper (not MTGO / Arena only); files without the availability column count as paper
                "paper": ("paper" in [x.strip() for x in (row.get("availability") or "").split(",")]) if "availability" in row
                         else row.get("isOnlineOnly") != "true",
            }
    f.close()
    return out


def load_legal(src):
    """MTGJSON uuid -> True for printings banned in Commander, from cardLegalities.csv (columns uuid, commander, ...)."""
    f = open_any(src)
    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline=""))
    if "commander" not in (reader.fieldnames or []) or "uuid" not in (reader.fieldnames or []):
        raise ValueError("missing uuid / commander columns")
    out = {row["uuid"]: True for row in reader if (row.get("commander") or "").strip().lower() == "banned"}
    f.close()
    return out


# ---------- Card jobs (the same rules the app's deck analysis uses), for the budget swap file ----------
TYPE_ORDER = ["Land", "Creature", "Planeswalker", "Battle", "Instant", "Sorcery", "Artifact", "Enchantment"]
RE_LANDSEARCH = re.compile(r"search your library for[^.]*\b(?:basic )?(?:land|forest|plains|island|swamp|mountain)(?: or (?:forest|plains|island|swamp|mountain))? cards?[^.]*onto the battlefield"
                           r"|put (?:a|up to \w+) land cards? from your hand onto the battlefield|play an additional land", re.I)
RE_TREASURE = re.compile(r"create[^.]*treasure tokens?", re.I)
RE_DRAW = re.compile(r"\bdraws? (?:an? |one |two |three |four |five |x |that many |\d+ |cards? equal)", re.I)
RE_REMOVAL = re.compile(r"(?:destroy|exile)s? (?:up to \w+ )?(?:another )?target (?!player|opponent|land)|deals? (?:x|\d+) damage to (?:target|any target|up to)"
                        r"|\bfights? (?:up to \w+ )?(?:another )?target|target creature gets -[\dx]+/-[\dx]+|return target (?:nonland )?(?:creature|permanent)[^.]* to (?:its owner's|their owners') hands?", re.I)
RE_WIPE = re.compile(r"sacrifices? all (?:other )?(?:colored |nonland |non\w+ )?(?:creatures|permanents|artifacts|enchantments)|(?:destroy|exile) all (?:other )?(?:non\w+ )?(?:creatures|permanents|artifacts|enchantments)"
                     r"|each creature gets -|creatures (?:your opponents control |you don't control )?get -[\dx]+/-[\dx]+|deals? (?:x|\d+) damage to each (?:other )?creature|return all (?:non\w+ )?(?:creatures|permanents)[^.]*hands?", re.I)
RE_COUNTER = re.compile(r"\bcounter target\b", re.I)
RE_PROTECT = re.compile(r"(?:gains?|has|have) [^.]*\b(?:hexproof|indestructible|shroud|protection from)|\bphases? out\b", re.I)
RE_TUTOR = re.compile(r"search your library for (?:a|an|up to \w+) (?!basic|land|forest|plains|island|swamp|mountain)[^.]*card", re.I)
RE_RECUR = re.compile(r"from your graveyard to (?:your hand|the battlefield)|return target [^.]*card from your graveyard", re.I)
RE_WIN = re.compile(r"you win the game|loses? the game|creatures you control (?:get \+|gain trample and get)|\binfect\b", re.I)
JOBS = (("ramp", None), ("draw", RE_DRAW), ("removal", RE_REMOVAL), ("wipe", RE_WIPE), ("protect", RE_PROTECT),
        ("recursion", RE_RECUR), ("win", RE_WIN), ("tutor", RE_TUTOR), ("counter", RE_COUNTER))
STOP = set("a an the of to and or your you its it this that each any target card cards creature creatures until end turn may with from for into on in at as is are be can't".split())


RE_LAND_TUTOR = re.compile(r"search your library for[^.]*\b(?:basic land|land cards?|lands?|plains|island|swamp|mountain|forest)\b", re.I)


def tag_jobs(rl, regex_jobs, text):
    """The jobs from Scryfall's oracle-tag letters (d.rl, Build 287), the same mapping the app uses (deck-analysis.js roleTagsOf).
    Recursion and win conditions have no tag here and stay with the rules text."""
    has = lambda k: k in rl
    out = []
    if has("r"):
        out.append("ramp")
    # card advantage counts as draw unless it is only a tutor or recursion
    if has("d") or (has("a") and not has("t") and not has("e")):
        out.append("draw")
    # Scryfall's removal also covers board wipes and counterspells: those count as removal only when their text reads so (Cyclonic Rift)
    if has("v") and ("removal" in regex_jobs or not (has("w") or has("c"))):
        out.append("removal")
    if has("w"):
        out.append("wipe")
    if has("p"):
        out.append("protect")
    if "recursion" in regex_jobs:
        out.append("recursion")
    if "win" in regex_jobs:
        out.append("win")
    # a land search (Cultivate) is ramp, not a tutor
    if has("t") and not all(RE_LAND_TUTOR.search(sn) for sn in re.findall(r"search your library[^.]*", text, re.I) or ["x"]):
        out.append("tutor")
    if has("c"):
        out.append("counter")
    return out


def card_jobs(d):
    """The jobs a card (its name-index details) does, using the app's rules: [] for lands and cards with no job.
    A card with Scryfall's oracle-tag letters (d.rl) takes its jobs from them (tag_jobs); the rules-text reading is the fallback."""
    faces = d.get("fc") or [d]
    ty = faces[0].get("ty") or d.get("ty") or ""
    primary = next((t for t in TYPE_ORDER if re.search(r"\b" + t + r"\b", ty)), "Other")
    if primary == "Land":
        return [], primary
    text = re.sub(r"\([^)]*\)", "", "\n".join(f.get("tx") or "" for f in faces))
    inst = primary in ("Instant", "Sorcery")
    mine = " ".join(sn for sn in re.split(r"(?<=[.\n])", text) if not re.search(r"its controller|that player|opponent", sn, re.I))
    out = []
    for job, rx in JOBS:
        if job == "ramp":
            if (d.get("pm") and not inst) or RE_LANDSEARCH.search(text) or RE_TREASURE.search(mine):
                out.append(job)
        elif rx.search(mine if job in ("protect", "recursion", "draw") else text):
            out.append(job)
    if d.get("rl"):
        out = tag_jobs(d["rl"], out, text)
    return out, primary


def text_words(d, name):
    faces = d.get("fc") or [d]
    text = "\n".join(f.get("tx") or "" for f in faces).lower().replace(name.lower(), "~")
    text = re.sub(r"\([^)]*\)", "", text)
    w = [x for x in re.findall(r"[a-z+/0-9{}~-]+", text) if x not in STOP]
    return set(w) | {a + " " + b for a, b in zip(w, w[1:])}


def write_swaps(root, index_shards, popular=10000, keep=8):
    """Budget swaps from the name index (see the docstring). Returns (cards with swaps, files)."""
    cards = []
    for ents in index_shards.values():
        for key, ent in ents.items():
            d = ent.get("d") or {}
            if d.get("bn") or not ent.get("p"):
                continue
            prices = [min(v for v in p["$"].values()) for p in ent["p"] if p.get("$")]
            if not prices:
                continue
            jobs, primary = card_jobs(d)
            if not jobs:
                continue
            cards.append({"k": key, "n": ent["n"], "c": min(prices), "mv": d.get("mv") or 0, "ci": "".join(d.get("ci") or []),
                          "jobs": set(jobs), "ty": "Spell" if primary in ("Instant", "Sorcery") else primary,
                          "kw": set(d.get("kw") or []), "rk": d.get("rk") or 0, "d": d})
    by_job = {}
    for c in cards:
        for j in c["jobs"]:
            by_job.setdefault(j, []).append(c)
    for lst in by_job.values():
        lst.sort(key=lambda c: c["c"])
    words = {}
    shards = {}
    count = 0
    for c in cards:
        if c["c"] < 50:
            continue
        # rarest job first keeps the pool small; every one of the card's jobs must be matched
        pool = min((by_job[j] for j in c["jobs"]), key=len)
        cap, ci = c["c"] * 0.5, set(c["ci"])
        cand = []
        for o in pool:
            if o["c"] > cap:
                break
            if o is c or not c["jobs"] <= o["jobs"] or not set(o["ci"]) <= ci or o["n"] == c["n"]:
                continue
            cand.append(o)
        if not cand:
            continue
        wc = words.get(c["k"]) or words.setdefault(c["k"], text_words(c["d"], c["n"]))
        scored = []
        for o in cand:
            wo = words.get(o["k"]) or words.setdefault(o["k"], text_words(o["d"], o["n"]))
            jac = len(wc & wo) / max(1, len(wc | wo))
            kw = len(c["kw"] & o["kw"]) / max(1, len(c["kw"] | o["kw"])) if (c["kw"] or o["kw"]) else 0
            sc = jac * 3 + kw + (1 if o["ty"] == c["ty"] else 0) - 0.1 * max(0, o["mv"] - c["mv"]) + (0.5 if 0 < o["rk"] <= popular else 0)
            scored.append((sc, o))
        scored.sort(key=lambda x: -x[0])
        pop = [o for _, o in scored if 0 < o["rk"] <= popular][:keep // 2]
        gem = [o for _, o in scored if not (0 < o["rk"] <= popular)][:keep // 2]
        best = sorted(pop + gem, key=lambda o: -next(s for s, x in scored if x is o))[:keep]
        shards.setdefault(c["k"].replace(" ", "")[:2] or "_", {})[c["k"]] = [
            [o["n"], o["c"], o["mv"], o["ci"], "p" if 0 < o["rk"] <= popular else "g", o["rk"]] for o in best]
        count += 1
    os.makedirs(root, exist_ok=True)
    for pre, ents in shards.items():
        with open(os.path.join(root, pre + ".json"), "w") as fh:
            json.dump(ents, fh, separators=(",", ":"), sort_keys=True)
    return count, len(shards)


# ---------- Card roles from Scryfall's oracle tags (Build 287): asked once a week, written to roles.json and each card's d.rl ----------
SCRYFALL_API = "https://api.scryfall.com"
SCRYFALL_HEADERS = {"User-Agent": "DeckShelfPriceData/1.0", "Accept": "application/json"}
# (letter, role, tags to ask for, fewest and most cards a tag may have). Every tag of a role that answers with a sensible size is used
# (aliases and narrower tags add up); the first one listed is the main one. Tag names come from Scryfall Tagger; the log lists each
# tag's total_cards on every refresh, so a renamed or missing tag shows up there (and keeps last week's letters for its role).
ROLE_TAGS = (
    ("r", "ramp", ("ramp",), 1000, 25000),
    ("v", "removal", ("removal",), 1000, 30000),
    ("a", "card advantage", ("card-advantage",), 1500, 40000),
    ("d", "draw", ("draw",), 800, 25000),
    ("w", "board wipe", ("sweeper", "board-wipe"), 100, 5000),
    ("t", "tutor", ("tutor",), 100, 6000),
    ("c", "counterspell", ("counterspell",), 100, 5000),
    ("p", "protection", ("protects-creature", "protects-permanent", "protection", "gives-protection", "gives-hexproof", "gives-indestructible"), 30, 10000),
    ("e", "recursion", ("recursion",), 200, 10000),
)
ROLES_MAX_AGE_DAYS = 7


def scryfall_get(url):
    """One Scryfall API page as JSON. A 404 (Scryfall's "no cards found", or a tag it doesn't know) returns None."""
    req = urllib.request.Request(url, headers=SCRYFALL_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_tag(tag, api=SCRYFALL_API, retries=2, wait=30, pause=0.1):
    """Every paper card with Scryfall oracle tag `tag`: (set of oracle ids, total_cards), or (None, 0) when Scryfall has no such cards.
    Follows has_more / next_page, pausing `pause` seconds between requests as Scryfall asks."""
    url = api.rstrip("/") + "/cards/search?" + urllib.parse.urlencode({"q": f"otag:{tag} game:paper", "unique": "cards", "order": "name"})
    ids, total, first = set(), 0, True
    while url:
        page = with_retries(f"roles: otag:{tag}", lambda: scryfall_get(url), retries, wait)
        if page is None or page.get("object") == "error":
            if first:
                return None, 0
            raise OSError(f"otag:{tag}: a later page was missing")
        if first:
            total = int(page.get("total_cards") or 0)
            first = False
        for c in page.get("data") or []:
            o = c.get("oracle_id") or next((f.get("oracle_id") for f in c.get("card_faces") or [] if f.get("oracle_id")), None)
            if o:
                ids.add(o)
        url = page.get("next_page") if page.get("has_more") else None
        if url:
            time.sleep(pause)
    return ids, total


def load_roles(path):
    """A roles.json written by an earlier run, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            r = json.load(fh)
        if isinstance(r, dict) and isinstance(r.get("c"), dict) and r.get("at"):
            return r
    except Exception:
        pass
    return None


def roles_age_days(r, now):
    try:
        at = dt.datetime.strptime(r["at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return (now - at).total_seconds() / 86400
    except Exception:
        return 1e9


def build_roles(prev, api=SCRYFALL_API, retries=2, wait=30, pause=0.1, now=None, fetch=None):
    """Ask Scryfall for every role's tags. A role whose tags all fail (unknown tag, error, a count outside its range) keeps the letter
    from `prev` (last week's roles). Returns the new roles dict, or None when no role answered (keep `prev` whole)."""
    fetch = fetch or (lambda tag: fetch_tag(tag, api, retries, wait, pause))
    now = now or dt.datetime.now(dt.timezone.utc)
    prev_c = (prev or {}).get("c") or {}
    letters, tags_seen, ok, answered = {}, {}, True, 0
    for letter, role, tags, lo, hi in ROLE_TAGS:
        got = None
        for tag in tags:
            try:
                ids, total = fetch(tag)
            except Exception as e:
                print(f"roles: otag:{tag} ({role}) failed ({e!r}); skipped", file=sys.stderr, flush=True)
                tags_seen[tag] = None
                continue
            tags_seen[tag] = total if ids is not None else None
            if ids is None:
                print(f"roles: otag:{tag} ({role}): Scryfall has no cards with this tag; skipped", flush=True)
                continue
            if not (lo <= total <= hi) or len(ids) < lo:
                print(f"roles: otag:{tag} ({role}): total_cards {total} (read {len(ids)}) is outside {lo}..{hi}; skipped", flush=True)
                continue
            print(f"roles: otag:{tag} ({role}): total_cards {total}, read {len(ids)}", flush=True)
            got = (got or set()) | ids
        if got is None:
            ok = False
            kept = [o for o, ls in prev_c.items() if letter in ls]
            print(f"roles: {role}: no tag answered; kept last week's {len(kept)} cards", flush=True)
            got = set(kept)
        else:
            answered += 1
        for o in got:
            letters[o] = letters.get(o, "") + letter
    if not answered:
        return None
    order = "".join(t[0] for t in ROLE_TAGS)
    c = {o: "".join(sorted(ls, key=order.index)) for o, ls in letters.items()}
    return {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "ok": ok, "tags": tags_seen, "c": c}


def roles_step(enabled, prev_dir, out_dir, api=SCRYFALL_API, retries=2, wait=30, pause=0.1, now=None, fetch=None):
    """This run's roles: last run's roles.json when it is under a week old and complete (or --roles is off), otherwise asked again;
    when asking fails the last good file is kept. Writes out_dir/roles.json and returns its dict (or None when there is none)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    prev = load_roles(os.path.join(prev_dir, "roles.json")) if prev_dir else None
    roles = prev
    if not enabled:
        if prev:
            print(f"roles: not asked this run; kept the roles from {prev['at']}", flush=True)
    elif prev and prev.get("ok") and roles_age_days(prev, now) < ROLES_MAX_AGE_DAYS:
        print(f"roles: the roles from {prev['at']} are under {ROLES_MAX_AGE_DAYS} days old; kept ({len(prev['c'])} cards)", flush=True)
    else:
        try:
            fresh = build_roles(prev, api, retries, wait, pause, now, fetch)
        except Exception as e:
            print(f"roles: asking Scryfall failed ({e!r})", file=sys.stderr, flush=True)
            fresh = None
        if fresh:
            roles = fresh
            print(f"roles: {len(fresh['c'])} cards with a role" + ("" if fresh["ok"] else " (some roles kept last week's cards)"), flush=True)
        elif prev:
            print(f"roles: no answer from Scryfall; kept the roles from {prev['at']}", file=sys.stderr, flush=True)
        else:
            print("roles: no answer from Scryfall and no earlier roles; cards are read from their text only", file=sys.stderr, flush=True)
    if roles:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "roles.json"), "w", encoding="utf-8") as fh:
            json.dump(roles, fh, separators=(",", ":"), sort_keys=True)
    return roles


def load_sets(src):
    """Set code -> {"n": set name, "d": release date, "t": set type, "ts": token set code} from MTGJSON sets.csv."""
    f = open_any(src)
    out = {}
    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline="")):
        if row.get("code"):
            out[row["code"].upper()] = {"n": row.get("name") or "", "d": row.get("releaseDate") or "", "t": row.get("type") or ""}
            if row.get("tokenSetCode"):
                out[row["code"].upper()]["ts"] = row["tokenSetCode"].upper()
    f.close()
    return out


# MTGJSON's language names -> Scryfall's language codes (a printing in English gets no code)
LANG_CODES = {"Japanese": "ja", "Chinese Simplified": "zhs", "Chinese Traditional": "zht", "Korean": "ko", "German": "de",
              "French": "fr", "Italian": "it", "Spanish": "es", "Portuguese (Brazil)": "pt", "Portuguese": "pt", "Russian": "ru",
              "Phyrexian": "ph", "Ancient Greek": "grc", "Latin": "la", "Hebrew": "he", "Arabic": "ar", "Sanskrit": "sa"}


def lang_code(name):
    """Scryfall's code for a MTGJSON language name; "" for English or no language. An unknown name gets a lower-case short form."""
    name = (name or "").strip()
    if not name or name.lower() == "english":
        return ""
    return LANG_CODES.get(name) or re.sub(r"[^a-z]+", "", name.lower())[:3] or ""


def printing_info(info, oracle_id, prices):
    """One printing for the name index: finishes (n/f/e/s), treatment tags, rarity letter, language code (not English), latest price per finish."""
    fins = "".join(c for w, c in (("nonfoil", "n"), ("foil", "f"), ("etched", "e"), ("signed", "s"))
                   if w in [x.strip() for x in info["fin"].split(",")])
    tags = []
    for tok in info["promo"].split(",") + info["frame"].split(","):
        tok = tok.strip()
        if tok and tok not in tags:
            tags.append(tok)
    if info["border"] and info["border"] != "black" and info["border"] not in tags:
        tags.append(info["border"])
    tags += [t for t in info["flags"] if t not in tags]
    ent = {"s": info["set"].upper(), "c": info["num"], "f": fins}
    if tags:
        ent["t"] = tags
    if info["rar"]:
        ent["r"] = info["rar"][0]
    lg = lang_code(info.get("lang"))
    if lg:
        ent["lg"] = lg
    latest = {}
    for fin, vals in prices.items():
        last = next((v for v in reversed(vals) if v is not None), None)
        if last is not None:
            latest[fin] = last
    if latest:
        ent["$"] = latest
    return ent


def inspect_csv(src, cap=60):
    """Describe a CSV for planning: its columns, the values of low-variety columns, and one sample row."""
    csv.field_size_limit(1 << 30)
    f = open_any(src)
    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline=""))
    cols = list(reader.fieldnames or [])
    seen = {c: set() for c in cols}
    sample, rows = None, 0
    for row in reader:
        rows += 1
        if sample is None:
            sample = {k: (v or "")[:80] for k, v in row.items() if k}
        for c in cols:
            s = seen[c]
            if s is not None:
                s.add(row.get(c) or "")
                if len(s) > cap:
                    seen[c] = None  # too many different values to be a category
    f.close()
    return {"rows": rows, "columns": cols, "sample": sample,
            "values": {c: sorted(s) for c, s in seen.items() if s is not None}}


NAMES_V = ""


def norm_name(s):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


BASIC_TYPES = {"Plains": "W", "Island": "U", "Swamp": "B", "Mountain": "R", "Forest": "G"}


def face_detail(info, multi=False, produced=None):
    """One face's (or a whole single-faced card's) details, empty values left out."""
    d = {}
    if multi:
        d["n"] = info["face"] or info["name"]
    for key, col in (("mc", "mc"), ("ty", "type"), ("tx", "text"), ("pw", "pw"), ("to", "to"), ("lo", "lo"), ("de", "de")):
        if info.get(col):
            d[key] = info[col]
    if multi and num(info.get("fmv")) is not None:
        d["mv"] = num(info["fmv"])
    co = split_list(info.get("colors"))
    if co:
        d["co"] = co
    if produced:
        d["pm"] = produced
    return d


def card_detail(infos):
    """Details of one card from its face rows (side a first). One face: the fields sit at the top; several: a "fc" list."""
    i0 = infos[0]
    multi = len(infos) > 1
    wide = split_list(i0.get("pm"))
    d = {}
    if i0.get("layout") and i0["layout"] != "normal":
        d["ly"] = i0["layout"]
    mv = num(i0.get("mv"))
    if mv is not None:
        d["mv"] = mv
    ci = split_list(i0.get("ci"))
    if ci:
        d["ci"] = ci
    kw = []
    for i in infos:
        for k in split_list(i.get("kw")):
            if k not in kw:
                kw.append(k)
    if kw:
        d["kw"] = kw
    if any(i.get("gc") for i in infos):
        d["gc"] = 1
    if any(i.get("bn") for i in infos):
        d["bn"] = 1   # banned in Commander
    ranks = [r for r in (num(i.get("rk")) for i in infos) if r]
    if ranks:
        d["rk"] = int(min(ranks))   # EDHREC rank (1 = most played)
    if multi:
        d["nm"] = i0["name"]
        d["fc"] = [face_detail(i, True) for i in infos]
        if wide:
            d["pm"] = wide
    else:
        d.update(face_detail(i0, False, wide))
    return d


def write_names(root, ids, names, recent, oracle, tcg=None, tokrel=None, ts_by_set=None, roles=None):
    """Write the name index: every paper printing (with its latest price when `recent`, id -> {finish: [prices]}, has one),
    plus any printing that has a price, with each card's details and each printing's extras. `roles` (oracle id -> role letters,
    from roles.json) adds "rl" to each card's details.
    Returns (printings, name files, set codes used, names with details, printings without a price)."""
    tcg, tokrel, ts_by_set, roles = tcg or {}, tokrel or {}, ts_by_set or {}, roles or {}
    bysid = {}
    for uuid, info in names.items():
        sid = ids.get(uuid)
        if sid and (sid in recent or info.get("paper", True)):
            bysid.setdefault(sid, []).append((uuid, info))
    unpriced = 0
    index, sets_used = {}, set()
    built = []
    for sid, rows in bysid.items():
        rows.sort(key=lambda r: r[1].get("side") or "")  # side a first
        uuid, info = rows[0]
        infos = [r[1] for r in rows]
        sets_used.add(info["set"].upper())
        pr = printing_info(info, oracle.get(uuid), recent.get(sid, {}))
        if "$" not in pr:
            unpriced += 1
        pr["i"] = sid
        fe = split_list(info["frame"])
        if fe:
            pr["e"] = fe
        prod = next((tcg[u] for u, _ in rows if tcg.get(u)), None)
        if prod:
            pr["u"] = prod
        if len((info.get("flavor") or "").strip()) > 1:
            pr["fn"] = info["flavor"].strip()
        faces = [i["face"] for i in infos if i["face"] and i["face"] != info["name"]]
        ts = ts_by_set.get(info["set"].upper())
        if ts:
            nums = []
            for nm in [info["name"]] + faces:
                for n_ in tokrel.get((ts, nm), []):
                    if n_ not in nums:
                        nums.append(n_)
            if nums:
                pr["k"] = nums
        detail = card_detail(infos)
        if roles.get(oracle.get(uuid)):
            detail["rl"] = roles[oracle[uuid]]   # Scryfall oracle-tag role letters (Build 287)
        built.append((oracle.get(uuid), info["name"], faces, detail, pr, not lang_code(info.get("lang"))))
    # Full card names first, then face names: a face name never joins a different card that already owns that key
    for faces_pass in (False, True):
        for orc, full, faces, detail, pr, english in built:
            for nm in (faces if faces_pass else [full]):
                key = norm_name(nm)
                if not key:
                    continue
                ent = index.get(key)
                if ent is None:
                    ent = index[key] = {"n": nm, "p": []}
                    if orc:
                        ent["o"] = orc
                    ent["d"] = detail
                    ent["_en"] = english
                elif faces_pass and ent.get("o") != orc:
                    continue  # a different card owns this name
                elif english and not ent.get("_en"):
                    ent["d"], ent["_en"] = detail, True  # the card's details come from an English printing when there is one
                ent["p"].append(pr)
    shards = {}
    names_list = []
    for key, ent in index.items():
        ent.pop("_en", None)
        ent["p"].sort(key=lambda p: (p["s"], p["c"].zfill(6), p["i"]))
        d = ent.get("d") or {}
        face0 = (d.get("fc") or [{}])[0]
        names_list.append([ent["n"], ent.get("o", ""), next((p["i"] for p in ent["p"] if not p.get("lg")), ent["p"][0]["i"]), d.get("mc") or face0.get("mc") or "",
                           d.get("ty") or face0.get("ty") or "", "".join(d.get("ci") or [])])
        shards.setdefault(key.replace(" ", "")[:2] or "_", {})[key] = ent
    os.makedirs(root, exist_ok=True)
    # names.json: one short row per card name [name, oracle id, a printing id for the picture, mana cost, type line, colour identity]
    names_list.sort(key=lambda r: norm_name(r[0]))
    blob = json.dumps(names_list, separators=(",", ":"), ensure_ascii=False)
    with open(os.path.join(os.path.dirname(os.path.abspath(root)), "names.json"), "w", encoding="utf-8") as fh:
        fh.write(blob)
    global NAMES_V
    NAMES_V = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]   # changes only when the list itself changes
    for pre, ents in shards.items():
        with open(os.path.join(root, pre + ".json"), "w") as fh:
            json.dump(ents, fh, separators=(",", ":"), sort_keys=True)
    global LAST_SHARDS
    LAST_SHARDS = shards
    return len(bysid), len(shards), sets_used, len(index), unpriced


LAST_SHARDS = None


def fin_letters(info):
    have = [x.strip() for x in info["fin"].split(",")]
    return "".join(c for w, c in (("nonfoil", "n"), ("foil", "f"), ("etched", "e"), ("signed", "s")) if w in have)


def write_tokens(root, ids, tok_names, recent, tcg=None):
    """Write t/<token set code>.json = {"<token number>": token} for every token printing with a Scryfall id.
    Returns (tokens, files, rel) where rel maps (token set code, card name) -> [token numbers that card makes]."""
    tcg = tcg or {}
    bysid = {}
    for uuid, info in tok_names.items():
        sid = ids.get(uuid)
        if sid and info.get("layout") != "art_series":
            bysid.setdefault(sid, []).append((uuid, info))
    sets, rel, count = {}, {}, 0
    for sid, rows in bysid.items():
        rows.sort(key=lambda r: r[1].get("side") or "")
        uuid, info = rows[0]
        infos = [r[1] for r in rows]
        multi = len(infos) > 1
        tok = {"i": sid, "n": info["name"], "f": fin_letters(info)}
        if info.get("layout") and info["layout"] != "token":
            tok["ly"] = info["layout"]
        kw = []
        for i in infos:
            for k in split_list(i.get("kw")):
                if k not in kw:
                    kw.append(k)
        if kw:
            tok["kw"] = kw
        ci = split_list(info.get("ci"))
        if ci:
            tok["ci"] = ci
        if multi:
            tok["fc"] = [face_detail(i, True) for i in infos]
        else:
            tok.update(face_detail(info, False, split_list(info.get("pm"))))
        prod = next((tcg[u] for u, _ in rows if tcg.get(u)), None)
        if prod:
            tok["u"] = prod
        latest = {}
        for fin, vals in (recent.get(sid) or {}).items():
            last = next((v for v in reversed(vals) if v is not None), None)
            if last is not None:
                latest[fin] = last
        if latest:
            tok["$"] = latest
        tset = re.sub(r"[^A-Za-z0-9_-]", "_", info["set"].upper())
        number = info["num"] or "_"
        bucket = sets.setdefault(tset, {})
        if number in bucket and bucket[number]["i"] != sid:
            number = number + "#" + sid[:4]
        bucket[number] = tok
        count += 1
        try:
            related = json.loads(info.get("rel") or "{}").get("reverseRelated") or []
        except ValueError:
            related = []
        for nm in related:
            lst = rel.setdefault((info["set"].upper(), nm), [])
            if number not in lst:
                lst.append(number)
    os.makedirs(root, exist_ok=True)
    for tset, bucket in sets.items():
        with open(os.path.join(root, tset + ".json"), "w") as fh:
            json.dump(bucket, fh, separators=(",", ":"), sort_keys=True)
    return count, len(sets), rel


def day_num(s):
    y, m, d = (int(x) for x in s.split("-"))
    return (dt.date(y, m, d) - dt.date(1970, 1, 1)).days


def write_shards(root, d0, cards_by_sid):
    """Write {sid: {fin: [cents|None]}} as shard files root/<first 3 chars>.json. Returns (cards, shards)."""
    shards = {}
    for sid, fins in cards_by_sid.items():
        shards.setdefault(sid[:3], {})[sid] = fins
    os.makedirs(root, exist_ok=True)
    for pre, cards in shards.items():
        with open(os.path.join(root, pre + ".json"), "w") as fh:
            json.dump({"d0": d0, "c": cards}, fh, separators=(",", ":"))
    return len(cards_by_sid), len(shards)


def read_prev(prev_dir):
    """The previously published recent files: (d0, last day, folder of shards) or None."""
    if not prev_dir:
        return None
    try:
        with open(os.path.join(prev_dir, "meta.json")) as fh:
            m = json.load(fh)
        sdir = os.path.join(prev_dir, "s")
        if not os.path.isdir(sdir):
            return None
        return int(m["d0"]), day_num(m["latest"]), sdir
    except Exception as e:
        print(f"previous data unusable ({e}); building without it", file=sys.stderr, flush=True)
        return None


def carry_names_forward(prev_dir, out_dir):
    """Today's name index could not be built (MTGJSON download cut off, bad file...): reuse the one published last time, so card search
    keeps working on slightly older names instead of finding nothing. Copies names.json, n/ and sets.json from the previous run's files into
    out_dir and returns the meta fields that describe them, or None when there is no usable previous index (then nothing is copied)."""
    if not prev_dir:
        return None
    try:
        with open(os.path.join(prev_dir, "meta.json")) as fh:
            pm = json.load(fh)
        if int(pm.get("nameFormat") or 0) < 3 or not pm.get("namesV"):
            return None
        src_n, src_list, src_sets = os.path.join(prev_dir, "n"), os.path.join(prev_dir, "names.json"), os.path.join(prev_dir, "sets.json")
        if not os.path.isdir(src_n) or not os.path.isfile(src_list):
            return None
        with open(src_list, encoding="utf-8") as fh:
            rows = json.load(fh)
        if not isinstance(rows, list) or not rows:
            return None
        shutil.copytree(src_n, os.path.join(out_dir, "n"))
        shutil.copy2(src_list, os.path.join(out_dir, "names.json"))
        if os.path.isfile(src_sets):
            shutil.copy2(src_sets, os.path.join(out_dir, "sets.json"))
        kept = {k: pm[k] for k in ("names", "nameFiles", "nameFormat", "namesV", "sets", "unpriced", "legalOk") if k in pm}
        kept["namesFrom"] = pm.get("namesFrom") or pm.get("built") or ""   # when this list was really built; stays the same while it is reused
        return kept
    except Exception as e:
        print(f"names: previous index not reusable ({e!r})", file=sys.stderr, flush=True)
        shutil.rmtree(os.path.join(out_dir, "n"), ignore_errors=True)
        for f in ("names.json", "sets.json"):
            try: os.remove(os.path.join(out_dir, f))
            except OSError: pass
        return None


def carry_banned_forward(prev_dir, names, oracle):
    """Today's ban list could not be downloaded: mark the cards that the previously published name index marked banned ("bn"), matched by
    oracle id (or by name), so a banned card doesn't suddenly look legal. Returns how many of today's printings were marked, or None when
    there is no usable previous index. Cards banned since then are not known, which is why meta.legalOk is false on such a day."""
    if not prev_dir:
        return None
    try:
        src_n = os.path.join(prev_dir, "n")
        if not os.path.isdir(src_n):
            return None
        orcs, keys = set(), set()
        for fn in os.listdir(src_n):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(src_n, fn), encoding="utf-8") as fh:
                for key, ent in json.load(fh).items():
                    if (ent.get("d") or {}).get("bn"):
                        keys.add(key)
                        if ent.get("o"):
                            orcs.add(ent["o"])
        marked = 0
        for u, info in names.items():
            if (oracle.get(u) and oracle[u] in orcs) or norm_name(info.get("name") or "") in keys:
                info["bn"] = True
                marked += 1
        return marked
    except Exception as e:
        print(f"legalities: earlier ban marks not readable ({e!r})", file=sys.stderr, flush=True)
        return None


def read_prices(src, ids, d0, n):
    """TCGplayer retail prices from AllPrices.json(.gz): ({scryfall id: {"n"|"f"|"e": array('i')}}, latest day with a price). -1 = no price."""
    series = {}
    latest = d0
    seen = 0
    f = open_any(src)
    try:
        for uuid, card in kvitems(f, "data", use_float=True):
            sid = ids.get(uuid)
            if not sid:
                continue
            retail = (((card.get("paper") or {}).get("tcgplayer") or {}).get("retail")) or {}
            for fin, key in FIN.items():
                pts = retail.get(fin)
                if not pts:
                    continue
                arr = None
                for date, price in pts.items():
                    if price is None:
                        continue
                    i = day_num(date) - d0
                    if i < 0 or i >= n:
                        continue
                    if arr is None:
                        s = series.setdefault(sid, {})
                        arr = s.get(key)
                        if arr is None:
                            arr = s[key] = array("i", [-1] * n)
                    if arr[i] == -1:  # first uuid wins; later faces only fill gaps
                        arr[i] = int(round(price * 100))
                        latest = max(latest, d0 + i)
            seen += 1
            if seen % 20000 == 0:
                print(f"  read {seen} cards", flush=True)
    finally:
        f.close()
    return series, latest


def make_block(k, B, series, d0, n, prev):
    """Assemble block k (days k*B .. k*B+B-1) as {sid: {fin: [cents|None]*B}}, or {} if no source has a price in it."""
    bs = k * B
    blk = {}

    def put(sid, fin, day, v):
        arr = blk.setdefault(sid, {}).get(fin)
        if arr is None:
            arr = blk[sid][fin] = array("i", [-1] * B)
        arr[day - bs] = v

    # 1) the previous run's recent files (they hold days MTGJSON's window has already dropped)
    if prev and prev[0] <= bs + B - 1 and prev[1] >= bs:
        for name in os.listdir(prev[2]):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(prev[2], name)) as fh:
                sh = json.load(fh)
            pd0 = sh["d0"]
            for sid, fins in sh["c"].items():
                for fin, vals in fins.items():
                    for j, v in enumerate(vals):
                        day = pd0 + j
                        if v is not None and bs <= day < bs + B:
                            put(sid, fin, day, v)
    # 2) MTGJSON's window wins where it has a price
    lo, hi = max(bs, d0), min(bs + B - 1, d0 + n - 1)
    if lo <= hi:
        for sid, fins in series.items():
            for fin, arr in fins.items():
                for day in range(lo, hi + 1):
                    v = arr[day - d0]
                    if v >= 0:
                        put(sid, fin, day, v)
    return {sid: {fin: [None if v < 0 else v for v in arr] for fin, arr in fins.items()} for sid, fins in blk.items()}


def update_archive(arch_dir, B, keep_days, last, cur, series, d0, n, prev):
    """Close every finished block that isn't archived yet, drop blocks past the retention window.
    Returns the sorted list of block numbers now in the archive."""
    root = os.path.join(arch_dir, "a")
    os.makedirs(root, exist_ok=True)
    have = {int(x) for x in os.listdir(root) if x.isdigit()}
    keep_from = last - keep_days + 1
    for k in sorted(have):
        if (k + 1) * B - 1 < keep_from:  # the whole block is older than we keep
            shutil.rmtree(os.path.join(root, str(k)))
            have.discard(k)
            print(f"archive: dropped block {k} (older than {keep_days} days)", flush=True)
    first_src = d0 if not prev else min(d0, prev[0])
    for k in range(max(first_src // B, keep_from // B), cur):
        if k in have:
            continue  # closed blocks never change
        cards = make_block(k, B, series, d0, n, prev)
        if not cards:
            continue
        c, s = write_shards(os.path.join(root, str(k)), k * B, cards)
        have.add(k)
        print(f"archive: closed block {k} (days {k * B}..{k * B + B - 1}): {c} cards in {s} shards", flush=True)
    return sorted(have)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", required=True, help="AllPrices.json(.gz), path or URL")
    ap.add_argument("--identifiers", required=True, nargs="+",
                    help="cardIdentifiers.csv(.gz) or AllIdentifiers.json(.gz); several = fallbacks in order")
    ap.add_argument("--extra-identifiers", nargs="*", default=[],
                    help="more identifier files merged in (e.g. tokenIdentifiers.csv); failures are ignored")
    ap.add_argument("--names", nargs="*", default=[],
                    help="cards.csv / tokens.csv (path or URL) to build the card-name index from; failures are ignored")
    ap.add_argument("--tokens", nargs="*", default=[],
                    help="tokens.csv (path or URL): written to t/<token set>.json and linked from cards; failures are ignored")
    ap.add_argument("--sets", help="sets.csv (path or URL) for set names and release dates, written to out/sets.json")
    ap.add_argument("--legalities", help="cardLegalities.csv (path or URL): marks cards banned in Commander; failures are ignored")
    ap.add_argument("--roles", action="store_true",
                    help="ask Scryfall for card roles (oracle tags) when last run's roles.json is a week old; without it the old file is kept")
    ap.add_argument("--roles-api", default=SCRYFALL_API, help="Scryfall API base (tests point it at a local server)")
    ap.add_argument("--inspect", nargs="*", default=[],
                    help="CSV files (path or URL) to describe in out/columns.json (columns, category values, sample row)")
    ap.add_argument("--out", required=True, help="folder for the recent files + meta.json (the `data` branch)")
    ap.add_argument("--days", type=int, default=90, help="how many days of MTGJSON prices to read")
    ap.add_argument("--today", help="YYYY-MM-DD (UTC); default is today")
    ap.add_argument("--block-days", type=int, default=90, help="archive block length; must not exceed --days")
    ap.add_argument("--keep-days", type=int, default=730, help="how long the archive keeps history (default 2 years)")
    ap.add_argument("--archive-dir", help="folder holding the archive branch checkout; finished blocks are added under a/")
    ap.add_argument("--retries", type=int, default=0, help="how many more times to try a download (prices, identifiers, names, tokens, sets, legalities) when it fails (default 0)")
    ap.add_argument("--retry-wait", type=int, default=600, help="seconds to wait between those tries (default 600 = 10 minutes)")
    ap.add_argument("--prev", help="folder with the previously published recent files (the old `data` branch)")
    ap.add_argument("--archive-base", help="public URL of the archive branch, written into meta.json for the app")
    a = ap.parse_args()

    B = a.block_days
    if B < 1 or B > a.days:
        sys.exit("--block-days must be between 1 and --days")
    today = a.today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    last = day_num(today)
    d0 = last - a.days + 1
    n = a.days
    cur = last // B          # the block that is still open
    rec0 = max(cur * B, d0)  # first day in the recent files

    ids = None
    oracle = {}  # uuid -> scryfall oracle id, used by the name index
    tcg = {}     # uuid -> TCGplayer product id
    for src in a.identifiers:
        try:
            ids = with_retries(f"identifiers: {src}", lambda: load_ids(src, oracle, tcg), a.retries, a.retry_wait)
            if ids:
                print(f"identifiers: {len(ids)} from {src}", flush=True)
                break
        except Exception as e:  # try the next source
            print(f"identifiers: {src} failed ({e})", file=sys.stderr, flush=True)
    if not ids:
        sys.exit("could not load any identifier source")
    for src in a.extra_identifiers:
        try:
            more = load_ids(src, oracle, tcg)
            ids.update(more)
            print(f"identifiers: +{len(more)} from {src}", flush=True)
        except Exception as e:
            print(f"identifiers: {src} skipped ({e})", file=sys.stderr, flush=True)

    # The main price file: read whole, from scratch on every try, so a download cut off midway is tried again instead of failing the run
    series, latest = with_retries(f"prices: {a.prices}", lambda: read_prices(a.prices, ids, d0, n), a.retries, a.retry_wait)

    # Archive first: it needs the OLD recent files (--prev) to still describe the block that just closed.
    arch = []
    if a.archive_dir:
        arch = update_archive(a.archive_dir, B, a.keep_days, last, cur, series, d0, n, read_prev(a.prev))

    recent = {}
    for sid, fins in series.items():
        out = {}
        for key, arr in fins.items():
            part = arr[rec0 - d0:]
            if any(v >= 0 for v in part):
                out[key] = [None if v < 0 else v for v in part]
        if out:
            recent[sid] = out

    out_dir = a.out
    shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir)
    cards, shards = write_shards(os.path.join(out_dir, "s"), rec0, recent)
    if a.inspect:
        report = {}
        for src in a.inspect:
            try:
                report[os.path.basename(src.split("?")[0])] = inspect_csv(src)
            except Exception as e:
                print(f"inspect: {src} skipped ({e})", file=sys.stderr, flush=True)
        with open(os.path.join(out_dir, "columns.json"), "w") as fh:
            json.dump(report, fh, indent=1)
        print(f"inspect: described {sorted(report)}", flush=True)
    # Card details, token files and the name index. None of this may stop prices from being published.
    names, tok_names = {}, {}
    for src in a.names:
        try:
            more = with_retries(f"names: {src}", lambda: load_names(src), a.retries, a.retry_wait)
            names.update(more)
            print(f"names: +{len(more)} from {src}", flush=True)
        except Exception as e:
            print(f"names: {src} skipped ({e})", file=sys.stderr, flush=True)
    for src in a.tokens:
        try:
            more = with_retries(f"tokens: {src}", lambda: load_names(src), a.retries, a.retry_wait)
            tok_names.update(more)
            print(f"tokens: +{len(more)} from {src}", flush=True)
        except Exception as e:
            print(f"tokens: {src} skipped ({e})", file=sys.stderr, flush=True)
    legal_ok = False   # True only when today's ban list was read; the app then knows a card without "bn" is not banned
    if a.legalities and names:
        try:
            banned = with_retries(f"legalities: {a.legalities}", lambda: load_legal(a.legalities), a.retries, a.retry_wait)
            for u in banned:
                if u in names:
                    names[u]["bn"] = True
            legal_ok = True
            print(f"legalities: {len(banned)} printings banned in Commander", flush=True)
        except Exception as e:
            print(f"legalities: {a.legalities} skipped ({e})", file=sys.stderr, flush=True)
            kept = carry_banned_forward(a.prev, names, oracle)
            if kept is None:
                print("legalities: no earlier ban marks to keep; no card is marked banned today", file=sys.stderr, flush=True)
            else:
                print(f"legalities: kept yesterday's ban marks ({kept} printings)", flush=True)
    all_sets = {}
    if a.sets:
        try:
            all_sets = with_retries(f"sets: {a.sets}", lambda: load_sets(a.sets), a.retries, a.retry_wait)
        except Exception as e:
            print(f"sets: {a.sets} skipped ({e})", file=sys.stderr, flush=True)
    roles = None
    try:
        roles = roles_step(a.roles, a.prev, out_dir, a.roles_api, min(a.retries, 2), min(a.retry_wait, 60))
    except Exception as e:
        print(f"roles: skipped ({e!r})", file=sys.stderr, flush=True)
    name_info, set_count, tok_info, tokrel = None, 0, None, {}
    try:
        if tok_names:
            tok_info = write_tokens(os.path.join(out_dir, "t"), ids, tok_names, recent, tcg)
            tokrel = tok_info[2]
            print(f"tokens: {tok_info[0]} tokens in {tok_info[1]} files", flush=True)
    except Exception as e:
        print(f"tokens: write failed ({e!r})", file=sys.stderr, flush=True)
        shutil.rmtree(os.path.join(out_dir, "t"), ignore_errors=True)
        tok_info = None
    try:
        if names:
            ts_by_set = {c: v["ts"] for c, v in all_sets.items() if v.get("ts")}
            name_info = write_names(os.path.join(out_dir, "n"), ids, names, recent, oracle, tcg, tokrel, ts_by_set, roles and roles.get("c"))
            print(f"names: {name_info[0]} printings in {name_info[1]} files, {name_info[3]} names, {name_info[4]} printings without a price", flush=True)
            used = {c: all_sets[c] for c in sorted(name_info[2]) if c in all_sets}
            if used:
                with open(os.path.join(out_dir, "sets.json"), "w") as fh:
                    json.dump(used, fh, separators=(",", ":"), sort_keys=True)
                set_count = len(used)
                print(f"sets: {set_count} of {len(name_info[2])} set codes found", flush=True)
    except Exception as e:
        print(f"names: write failed ({e!r})", file=sys.stderr, flush=True)
        shutil.rmtree(os.path.join(out_dir, "n"), ignore_errors=True)
        try: os.remove(os.path.join(out_dir, "names.json"))
        except OSError: pass
        name_info = None
    swap_info = None
    try:
        if name_info and LAST_SHARDS:
            t0 = time.time()
            swap_info = write_swaps(os.path.join(out_dir, "w"), LAST_SHARDS)
            print(f"swaps: {swap_info[0]} cards with budget swaps in {swap_info[1]} files ({time.time() - t0:.0f}s)", flush=True)
    except Exception as e:
        print(f"swaps: write failed ({e!r})", file=sys.stderr, flush=True)
        shutil.rmtree(os.path.join(out_dir, "w"), ignore_errors=True)
        swap_info = None
    kept_names = None
    if not name_info:
        kept_names = carry_names_forward(a.prev, out_dir)
        if kept_names:
            print(f"names: today's list could not be built, kept the list from {kept_names.get('namesFrom') or 'the last run'} ({kept_names.get('names', '?')} printings)", flush=True)
        else:
            print("names: today's list could not be built and there is no earlier list to keep; card search will have no name index", file=sys.stderr, flush=True)
    epoch = dt.date(1970, 1, 1)
    meta = {
        "latest": (epoch + dt.timedelta(days=max(latest, rec0))).isoformat(),
        "d0": rec0, "days": last - rec0 + 1, "block": B, "arch": arch, "keep": a.keep_days,
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cards": cards, "shards": shards,
    }
    if name_info:
        meta["names"], meta["nameFiles"], meta["nameFormat"] = name_info[0], name_info[1], 3
        meta["unpriced"] = name_info[4]   # paper printings listed without a price (shown as "No price" in the app)
        if NAMES_V:
            meta["namesV"] = NAMES_V
        meta["legalOk"] = legal_ok
        if set_count:
            meta["sets"] = set_count
    if kept_names:
        meta.update(kept_names)
    if tok_info:
        meta["tokens"], meta["tokenSets"] = tok_info[0], tok_info[1]
    if swap_info:
        meta["swaps"], meta["swapFiles"] = swap_info[0], swap_info[1]
    if roles:
        meta["rolesAt"], meta["rolesOk"], meta["roles"] = roles["at"], bool(roles.get("ok")), len(roles["c"])
    if a.archive_base:
        meta["archive"] = a.archive_base if a.archive_base.endswith("/") else a.archive_base + "/"
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))
    print("done", json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
