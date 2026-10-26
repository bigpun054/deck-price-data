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
                   "$": {"n"|"f"|"e": latest TCGplayer price in US cents}}  (finishes with a price only)
    Lists every printing that has price data, so a card name can be turned into its printings and ids.
    sets.json     (needs --sets) {"<SET>": {"n": set name, "d": release date, "t": set type}} for the sets used above.
    The normalized name is lowercase letters and digits with single spaces ("sol ring"); the first 2 letters/digits
    of it (spaces dropped) pick the file. Double-faced cards are listed under the full name and under each face name.

  cards (inside the same name files, format 3)
    each name entry also carries "d", the card's details (rules text, type, mana cost, colours ...), and each printing may carry
    "e" (frame effects), "u" (TCGplayer product id), "fn" (flavour name) and "k" (numbers of the tokens it makes, see below).
      d = {"ty": type line, "tx": rules text, "mc": mana cost, "mv": mana value, "co": [colours], "ci": [colour identity],
           "kw": [keywords], "pw": power, "to": toughness, "lo": loyalty, "de": defense, "pm": [mana it can produce],
           "ly": layout (left out for "normal"), "gc": 1 for a Game Changer}
      a card with several faces carries "nm" (the full name) and "fc" = [{"n": face name, "mc", "mv", "ty", "tx", "pw", "to", "lo", "de", "co", "pm"}, ...]
      instead of the per-face fields, and keeps "mv", "ci", "kw", "ly", "gc" for the whole card.
  tokens (needs --tokens)
    t/<token set code>.json   {"<token number>": {"i": scryfall id, "n": name, "ty", "tx", "pw", "to", "co", "kw", "ly", "u", "f", "$", "fc"}}
    A printing's "k" lists the numbers of the tokens (in its set's token set, from sets.json "ts") that its card makes.

Day number = whole days since 1970-01-01 (UTC). n = nonfoil, f = foil, e = etched. Prices are US cents (TCGplayer retail).
MTGJSON only publishes the last 90 days, so an archive block is assembled from the previous run's recent files (--prev)
plus MTGJSON's window; days neither source has stay null.
"""
import argparse, csv, datetime as dt, gzip, io, json, os, re, shutil, sys, tempfile, urllib.request
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
            shutil.copyfileobj(r, tmp, 1 << 20)
        tmp.close()
        path = tmp.name
    return gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")



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
                "rel": row.get("relatedCards") or "",
            }
    f.close()
    return out


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


def printing_info(info, oracle_id, prices):
    """One printing for the name index: finishes (n/f/e/s), treatment tags, rarity letter, latest price per finish."""
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
    if multi:
        d["nm"] = i0["name"]
        d["fc"] = [face_detail(i, True) for i in infos]
        if wide:
            d["pm"] = wide
    else:
        d.update(face_detail(i0, False, wide))
    return d


def write_names(root, ids, names, recent, oracle, tcg=None, tokrel=None, ts_by_set=None):
    """Write the name index for the scryfall ids in `recent` (id -> {finish: [prices]}), with each card's details and
    each printing's extras. Returns (printings, name files, set codes used, names with details)."""
    tcg, tokrel, ts_by_set = tcg or {}, tokrel or {}, ts_by_set or {}
    bysid = {}
    for uuid, info in names.items():
        sid = ids.get(uuid)
        if sid and sid in recent:
            bysid.setdefault(sid, []).append((uuid, info))
    index, sets_used = {}, set()
    built = []
    for sid, rows in bysid.items():
        rows.sort(key=lambda r: r[1].get("side") or "")  # side a first
        uuid, info = rows[0]
        infos = [r[1] for r in rows]
        sets_used.add(info["set"].upper())
        pr = printing_info(info, oracle.get(uuid), recent[sid])
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
        built.append((oracle.get(uuid), info["name"], faces, card_detail(infos), pr))
    # Full card names first, then face names: a face name never joins a different card that already owns that key
    for faces_pass in (False, True):
        for orc, full, faces, detail, pr in built:
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
                elif faces_pass and ent.get("o") != orc:
                    continue  # a different card owns this name
                ent["p"].append(pr)
    shards = {}
    for key, ent in index.items():
        ent["p"].sort(key=lambda p: (p["s"], p["c"].zfill(6), p["i"]))
        shards.setdefault(key.replace(" ", "")[:2] or "_", {})[key] = ent
    os.makedirs(root, exist_ok=True)
    for pre, ents in shards.items():
        with open(os.path.join(root, pre + ".json"), "w") as fh:
            json.dump(ents, fh, separators=(",", ":"), sort_keys=True)
    return len(bysid), len(shards), sets_used, len(index)


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
    ap.add_argument("--inspect", nargs="*", default=[],
                    help="CSV files (path or URL) to describe in out/columns.json (columns, category values, sample row)")
    ap.add_argument("--out", required=True, help="folder for the recent files + meta.json (the `data` branch)")
    ap.add_argument("--days", type=int, default=90, help="how many days of MTGJSON prices to read")
    ap.add_argument("--today", help="YYYY-MM-DD (UTC); default is today")
    ap.add_argument("--block-days", type=int, default=90, help="archive block length; must not exceed --days")
    ap.add_argument("--keep-days", type=int, default=730, help="how long the archive keeps history (default 2 years)")
    ap.add_argument("--archive-dir", help="folder holding the archive branch checkout; finished blocks are added under a/")
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
            ids = load_ids(src, oracle, tcg)
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

    series = {}  # scryfall id -> {"n"|"f"|"e": array('i')}, -1 = no price
    latest = d0
    seen = 0
    f = open_any(a.prices)
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
    f.close()

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
            more = load_names(src)
            names.update(more)
            print(f"names: +{len(more)} from {src}", flush=True)
        except Exception as e:
            print(f"names: {src} skipped ({e})", file=sys.stderr, flush=True)
    for src in a.tokens:
        try:
            more = load_names(src)
            tok_names.update(more)
            print(f"tokens: +{len(more)} from {src}", flush=True)
        except Exception as e:
            print(f"tokens: {src} skipped ({e})", file=sys.stderr, flush=True)
    all_sets = {}
    if a.sets:
        try:
            all_sets = load_sets(a.sets)
        except Exception as e:
            print(f"sets: {a.sets} skipped ({e})", file=sys.stderr, flush=True)
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
            name_info = write_names(os.path.join(out_dir, "n"), ids, names, recent, oracle, tcg, tokrel, ts_by_set)
            print(f"names: {name_info[0]} printings in {name_info[1]} files, {name_info[3]} names", flush=True)
            used = {c: all_sets[c] for c in sorted(name_info[2]) if c in all_sets}
            if used:
                with open(os.path.join(out_dir, "sets.json"), "w") as fh:
                    json.dump(used, fh, separators=(",", ":"), sort_keys=True)
                set_count = len(used)
                print(f"sets: {set_count} of {len(name_info[2])} set codes found", flush=True)
    except Exception as e:
        print(f"names: write failed ({e!r})", file=sys.stderr, flush=True)
        shutil.rmtree(os.path.join(out_dir, "n"), ignore_errors=True)
        name_info = None
    epoch = dt.date(1970, 1, 1)
    meta = {
        "latest": (epoch + dt.timedelta(days=max(latest, rec0))).isoformat(),
        "d0": rec0, "days": last - rec0 + 1, "block": B, "arch": arch, "keep": a.keep_days,
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cards": cards, "shards": shards,
    }
    if name_info:
        meta["names"], meta["nameFiles"], meta["nameFormat"] = name_info[0], name_info[1], 3
        if set_count:
            meta["sets"] = set_count
    if tok_info:
        meta["tokens"], meta["tokenSets"] = tok_info[0], tok_info[1]
    if a.archive_base:
        meta["archive"] = a.archive_base if a.archive_base.endswith("/") else a.archive_base + "/"
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))
    print("done", json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
