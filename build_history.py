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

  names (optional, rebuilt every day, in the same `data` branch; needs --names)
    n/<first 2 letters/digits of the name, "_" if none>.json
                  {"<normalized name>": {"n": "<card name>", "p": [["<scryfall id>", "<SET>", "<collector number>"], ...]}}
    Lists every printing that has price data, so a card name can be turned into its printings and ids.
    The normalized name is lowercase letters and digits with single spaces ("sol ring"); the first 2 letters/digits
    of it (spaces dropped) pick the file. Double-faced cards are listed under the full name and under each face name.

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


def load_ids(src):
    """MTGJSON uuid -> Scryfall id. Accepts cardIdentifiers.csv(.gz) or AllIdentifiers.json(.gz)."""
    f = open_any(src)
    ids = {}
    if src.split("?")[0].endswith((".csv", ".csv.gz")):
        for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline="")):
            u, s = row.get("uuid"), row.get("scryfallId")
            if u and s:
                ids[u] = s
    else:
        for u, card in kvitems(f, "data"):
            s = (card.get("identifiers") or {}).get("scryfallId")
            if s:
                ids[u] = s
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
            out[u] = (row["name"], row.get("setCode") or "", row.get("number") or "", row.get("faceName") or "")
    f.close()
    return out


def norm_name(s):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


def write_names(root, ids, names, wanted):
    """Write the name index for scryfall ids in `wanted`. Returns (printings, name files)."""
    index, seen = {}, set()
    for uuid, (name, setc, num, face) in names.items():
        sid = ids.get(uuid)
        if not sid or sid not in wanted or sid in seen:
            continue
        seen.add(sid)
        for nm in {name, face} - {""}:
            key = norm_name(nm)
            if key:
                index.setdefault(key, {"n": nm, "p": []})["p"].append([sid, setc.upper(), num])
    shards = {}
    for key, ent in index.items():
        ent["p"].sort(key=lambda p: (p[1], p[2].zfill(6), p[0]))
        shards.setdefault(key.replace(" ", "")[:2] or "_", {})[key] = ent
    os.makedirs(root, exist_ok=True)
    for pre, ents in shards.items():
        with open(os.path.join(root, pre + ".json"), "w") as fh:
            json.dump(ents, fh, separators=(",", ":"), sort_keys=True)
    return len(seen), len(shards)


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
    for src in a.identifiers:
        try:
            ids = load_ids(src)
            if ids:
                print(f"identifiers: {len(ids)} from {src}", flush=True)
                break
        except Exception as e:  # try the next source
            print(f"identifiers: {src} failed ({e})", file=sys.stderr, flush=True)
    if not ids:
        sys.exit("could not load any identifier source")
    for src in a.extra_identifiers:
        try:
            more = load_ids(src)
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
    names = {}
    for src in a.names:
        try:
            more = load_names(src)
            names.update(more)
            print(f"names: +{len(more)} from {src}", flush=True)
        except Exception as e:  # the price data must still be published without names
            print(f"names: {src} skipped ({e})", file=sys.stderr, flush=True)
    name_info = None
    if names:
        name_info = write_names(os.path.join(out_dir, "n"), ids, names, set(recent))
        print(f"names: {name_info[0]} printings in {name_info[1]} files", flush=True)
    epoch = dt.date(1970, 1, 1)
    meta = {
        "latest": (epoch + dt.timedelta(days=max(latest, rec0))).isoformat(),
        "d0": rec0, "days": last - rec0 + 1, "block": B, "arch": arch, "keep": a.keep_days,
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cards": cards, "shards": shards,
    }
    if name_info:
        meta["names"], meta["nameFiles"] = name_info
    if a.archive_base:
        meta["archive"] = a.archive_base if a.archive_base.endswith("/") else a.archive_base + "/"
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))
    print("done", json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
