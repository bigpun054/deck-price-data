#!/usr/bin/env python3
"""Build compact 90-day TCGplayer price-history shards from MTGJSON, keyed by Scryfall id.

Output layout (served as static files):
  meta.json            {"latest": "YYYY-MM-DD", "d0": <day number of index 0>, "days": 90, "built": ..., "cards": N}
  s/<first 3 hex of scryfall id>.json
                       {"d0": <day number of index 0>, "c": {"<scryfall id>": {"n": [cents|null,...], "f": [...], "e": [...]}}}
Day number = whole days since 1970-01-01 (UTC). n = nonfoil, f = foil, e = etched. Prices are US cents (TCGplayer retail).
"""
import argparse, csv, datetime as dt, gzip, io, json, os, shutil, sys, tempfile, urllib.request
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


def day_num(s):
    y, m, d = (int(x) for x in s.split("-"))
    return (dt.date(y, m, d) - dt.date(1970, 1, 1)).days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", required=True, help="AllPrices.json(.gz), path or URL")
    ap.add_argument("--identifiers", required=True, nargs="+",
                    help="cardIdentifiers.csv(.gz) or AllIdentifiers.json(.gz); several = fallbacks in order")
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--today", help="YYYY-MM-DD (UTC); default is today")
    a = ap.parse_args()

    today = a.today or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    last = day_num(today)
    d0 = last - a.days + 1
    n = a.days

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

    shards = {}
    for sid, fins in series.items():
        shards.setdefault(sid[:3], {})[sid] = {k: [None if v < 0 else v for v in arr] for k, arr in fins.items()}

    out = a.out
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(os.path.join(out, "s"))
    for pre, cards in shards.items():
        with open(os.path.join(out, "s", pre + ".json"), "w") as fh:
            json.dump({"d0": d0, "c": cards}, fh, separators=(",", ":"))
    epoch = dt.date(1970, 1, 1)
    meta = {
        "latest": (epoch + dt.timedelta(days=latest)).isoformat(),
        "d0": d0, "days": n,
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cards": len(series), "shards": len(shards),
    }
    with open(os.path.join(out, "meta.json"), "w") as fh:
        json.dump(meta, fh, separators=(",", ":"))
    print("done", json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
