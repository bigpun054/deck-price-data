#!/usr/bin/env python3
"""Build the combo data for the Deck Shelf app from Commander Spellbook's daily export (commanderspellbook.com).

Spellbook publishes every combo ("variant") as one file, https://json.commanderspellbook.com/variants.json.gz, refreshed
periodically, and asks tools to download that file on their own schedule instead of paging their API. This script keeps every
field of it (nothing is trimmed) but stores each card, template, feature and combo recipe once instead of inside every variant,
and splits the variants into small files so the phone only downloads combos that use cards the player owns.

Output (in --out, published with the price files on the `data` branch under combo/):
  combo/meta.json        {"format", "timestamp", "version", "etag", "built", "variants", "aliases", "cards", "templates", "features",
                          "combos", "buckets", "indexFiles", "index": {"<index file>": content hash}, "tindex": {"<template id>": hash}, "names": hash of names.json, "formats": [every legality
                          format], "templateMatches", "matchedOn", "source", "credit"}
  combo/i/<first 2 letters/digits of the normalized card name>.json   (same file names as the price repo's n/ index)
      {"<normalized name>": {"c": Spellbook card id,
                             "v": [[combo id, [piece card ids], [[template id, how many], ...], [feature ids], mana value needed,
                                    bracket tag, colour identity, [indexes of pieces that must be the commander], popularity
                                    (decks on EDHREC that play it)], ...],
                             "t": [ids of the templates the card fits]}}
      Everything the phone needs to find complete and one-card-away combos for the cards it owns (names.json turns piece ids into
      names, features.json feature ids into results). Commander indexes count the
      named pieces first, then the template slots. A double-faced card is listed under its full name and each face's name.
  combo/tv/<template id>.json   [compact records of every combo that needs that template]   (meta "tindex": a hash per file)
      So a combo is found from a card that only fills one of its template slots, and combos made only of template slots are found too.
  combo/v/<first 3 hex of sha1(combo id)>.json   {"<combo id>": <the full combo>}  read when a combo's details are opened
      Spellbook's variant object with every field. To keep the files small: uses[].card is the card id (with the name in
      uses[].n; cards.json has the rest), requires[].template the template id (name in requires[].n), produces[].feature
      the feature id, of / includes are recipe ids, legalities is the list of formats it is legal in (meta "formats" has
      them all), and empty values (null, "", [], false, quantity 1) are left out.
  combo/names.json       {"<card id>": card name}   (meta "names" is its hash, so the phone re-reads it only when it changes)
  combo/cards.json       {"<card id>": <Spellbook card object>}
  combo/templates.json   {"<template id>": <Spellbook template object>}   ("any free sac outlet" style slots)
  combo/features.json    {"<feature id>": <Spellbook feature object>}     (results such as "Infinite mana", "Win the game")
  combo/recipes.json     {"<combo id>": <Spellbook combo object>}         (the recipes a variant is "of" or "includes")
  combo/aliases.json     [<Spellbook variant alias>, ...]                 (old combo ids that redirect to current ones)
  combo/matches.json     {"<template id>": {"q": query, "d": day searched, "m": [card names]}}

Template matching: a template stands for "any card that ...". For one written as a Scryfall-style query, the query is run on
Scryfall's search API (the same link Spellbook publishes as scryfallApi); for one written as a list of cards, Spellbook's
/cards/?matched_by=<id> is read. The matches are kept and only searched again when a template's query changed, a new template
appeared, or the last search is older than --match-days.

"Skips when unchanged": the download sends the previous ETag, and a file whose timestamp matches the previous one is not rebuilt;
the previous combo/ folder (from --prev) is copied instead.
"""
import argparse, datetime as dt, gzip, hashlib, json, os, shutil, sys, time, urllib.parse, urllib.request
from collections import defaultdict

try:
    import ijson
except ImportError:
    ijson = None

from build_history import norm_name

EXPORT = "https://json.commanderspellbook.com/variants.json.gz"
SPELLBOOK_API = "https://backend.commanderspellbook.com"
UA = "DeckShelf-data/1.0 (+https://github.com/bigpun054/deck-price-data)"
FORMAT = 2   # 2: short records carry every feature and the popularity


def log(*a):
    print("combos:", *a, file=sys.stderr, flush=True)


def prefix_of(norm):
    p = norm.replace(" ", "")[:2]
    return p if p else "_"


def http_get(url, headers=None, timeout=120):
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    return urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)


def download_export(src, etag, dest):
    """Download the export to dest. Returns (status, etag): status 'unchanged' when the server answers 304 to our ETag."""
    if not src.startswith(("http://", "https://")):
        shutil.copyfile(src, dest)
        return "ok", None
    try:
        r = http_get(src, {"If-None-Match": etag} if etag else None, timeout=900)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "unchanged", etag
        raise
    got = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
    want = r.headers.get("Content-Length")
    if want and want.isdigit() and got != int(want):
        raise OSError(f"download cut off: got {got} of {want} bytes")
    return "ok", r.headers.get("ETag")


def open_export(path):
    with open(path, "rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if gz else open(path, "rb")


def read_export(path):
    """(timestamp, version, variants iterator factory, aliases). Streams the variants so the whole file is never held as text."""
    head = {}
    with open_export(path) as fh:
        if ijson:
            for k, ev, val in ijson.parse(fh):
                if k in ("timestamp", "version") and ev in ("string", "number"):
                    head[k] = val
                if "timestamp" in head and "version" in head:
                    break
        else:
            d = json.load(fh)
            head = {"timestamp": d.get("timestamp"), "version": d.get("version")}

    def variants():
        with open_export(path) as fh:
            if ijson:
                yield from ijson.items(fh, "variants.item", use_float=True)
            else:
                yield from json.load(fh).get("variants", [])

    def aliases():
        with open_export(path) as fh:
            if ijson:
                return list(ijson.items(fh, "aliases.item", use_float=True))
            return json.load(fh).get("aliases", [])
    return head.get("timestamp"), head.get("version"), variants, aliases


def card_key(card):
    k = card.get("id")
    return str(k) if k is not None else str(card.get("oracleId") or card.get("name"))


def names_for(card_name):
    """The normalized names a card is indexed under: the full name, and each face of a multi-faced card."""
    out = [norm_name(card_name)]
    if "//" in card_name:
        out += [norm_name(p) for p in card_name.split("//")]
    return [n for n in dict.fromkeys(out) if n]


def bucket_of(vid):
    return hashlib.sha1(str(vid).encode()).hexdigest()[:3]


def slim(d, defaults):
    """Drop values that carry nothing (None, "", [], False, and the listed defaults). Readers treat a missing key as that default,
    so no information is lost; it only keeps 118,000 copies of empty fields out of the files."""
    for k in [k for k, v in d.items() if v is None or v == "" or v == [] or v is False or defaults.get(k, object()) == v]:
        del d[k]
    return d


def split_variants(variants_iter, formats):
    """Pull the shared objects out of every variant and build two views of it:
       full[bucket][id]  every field (details, read when a combo is opened)
       short[id]         the compact match record the index carries: [id, piece card ids, [[template id, qty], ...], feature ids,
                         mana value needed, bracket tag, colour identity, indexes of pieces that must be the commander]"""
    cards, templates, features, recipes = {}, {}, {}, {}
    full, short = defaultdict(dict), {}
    uses_by_name = defaultdict(set)
    sizes = defaultdict(int)
    count = 0
    for v in variants_iter:
        count += 1
        pieces, cmd = [], []
        for i, u in enumerate(v.get("uses") or []):
            c = u.get("card") or {}
            k = card_key(c)
            cards.setdefault(k, c)
            u["card"], u["n"] = (int(k) if k.isdigit() else k), c.get("name")
            pieces.append(u["card"])
            if u.get("mustBeCommander"):
                cmd.append(i)
            for nm in names_for(c.get("name") or ""):
                uses_by_name[nm].add(v["id"])
            slim(u, {"quantity": 1})
        req = []
        for r in v.get("requires") or []:
            t = r.get("template") or {}
            templates.setdefault(str(t.get("id")), t)
            r["template"], r["n"] = t.get("id"), t.get("name")
            req.append([t.get("id"), r.get("quantity") or 1])
            if r.get("mustBeCommander"):
                cmd.append(len(pieces) + len(req) - 1)
            slim(r, {"quantity": 1})
        feats = []
        for p in v.get("produces") or []:
            f = p.get("feature") or {}
            features.setdefault(str(f.get("id")), f)
            p["feature"] = f.get("id")
            p.pop("n", None)
            feats.append(f.get("id"))
            slim(p, {"quantity": 1})
        for key in ("of", "includes"):
            ids = []
            for c in v.get(key) or []:
                recipes.setdefault(str(c.get("id")), c)
                ids.append(c.get("id"))
            v[key] = ids
        lg = v.get("legalities")
        if isinstance(lg, dict):
            for f in lg:
                formats.setdefault(f, None)
            v["legalities"] = sorted(f for f, ok in lg.items() if ok)
        bt = v.get("bracketTag")
        short[v["id"]] = [v["id"], pieces, req, feats, v.get("manaValueNeeded"), bt if bt is not None else "", v.get("identity") or "", cmd,
                          v.get("popularity") or 0]
        slim(v, {})
        for k2, val in v.items():
            sizes[k2] += len(json.dumps(val, separators=(",", ":"), ensure_ascii=False))
        full[bucket_of(v["id"])][v["id"]] = v
    return count, cards, templates, features, recipes, full, short, uses_by_name, sizes


def scryfall_names(url, pause=0.12, max_pages=400):
    """Every card name a Scryfall search returns (all pages)."""
    out, pages = set(), 0
    while url and pages < max_pages:
        try:
            r = http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:          # Scryfall answers 404 to a search that finds nothing
                return out
            if e.code == 429:
                time.sleep(5)
                continue
            raise
        d = json.load(r)
        for c in d.get("data") or []:
            out.add(c.get("name") or "")
        url = d.get("next_page") if d.get("has_more") else None
        pages += 1
        time.sleep(pause)
    if url:
        raise RuntimeError(f"more than {max_pages} pages")
    return out


def spellbook_names(tid, pause=0.8):
    """Card names Spellbook lists for a template written as a list of cards."""
    out = set()
    url = f"{SPELLBOOK_API}/cards/?" + urllib.parse.urlencode({"matched_by": tid, "limit": 100})
    while url:
        try:
            d = json.load(http_get(url))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(int(e.headers.get("Retry-After") or 10))
                continue
            raise
        for c in d.get("results") or []:
            out.add(c.get("name") or "")
        url = d.get("next")
        time.sleep(pause)
    return out


def match_templates(templates, prev_matches, today, match_days, budget_s):
    """{template id: {"q": query used, "d": day searched, "m": [card names]}}. Reuses previous matches that are still fresh."""
    out, t0, searched, failed = {}, time.time(), 0, 0
    order = sorted(templates.items(), key=lambda kv: (kv[0] in prev_matches, (prev_matches.get(kv[0]) or {}).get("d", "")))
    for tid, t in order:
        q = t.get("scryfallQuery")
        old = prev_matches.get(tid)
        fresh = old and old.get("q") == q and old.get("d") and \
            (dt.date.fromisoformat(today) - dt.date.fromisoformat(old["d"])).days < match_days
        if fresh or (old and old.get("q") == q and time.time() - t0 > budget_s):
            out[tid] = old
            continue
        if time.time() - t0 > budget_s:
            continue                   # out of time for new searches today; picked up tomorrow
        try:
            names = scryfall_names(t["scryfallApi"]) if q is not None and t.get("scryfallApi") else spellbook_names(tid)
            out[tid] = {"q": q, "d": today, "m": sorted(n for n in names if n)}
            searched += 1
        except Exception as e:
            failed += 1
            log(f"template {tid} ({t.get('name')}) search failed: {e!r}")
            if old:
                out[tid] = old
    log(f"templates: {len(out)} of {len(templates)} matched, {searched} searched today, {failed} failed ({time.time() - t0:.0f}s)")
    return out


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)
    return os.path.getsize(path)


def read_prev(prev_dir):
    meta, matches = {}, {}
    if not prev_dir:
        return meta, matches
    try:
        with open(os.path.join(prev_dir, "combo", "meta.json")) as fh:
            meta = json.load(fh)
    except Exception:
        pass
    try:
        with open(os.path.join(prev_dir, "combo", "matches.json")) as fh:
            matches = json.load(fh)
    except Exception:
        pass
    return meta, matches


def carry_forward(prev_dir, out_dir):
    src = os.path.join(prev_dir or "", "combo")
    if not prev_dir or not os.path.isfile(os.path.join(src, "meta.json")):
        return None
    dst = os.path.join(out_dir, "combo")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    with open(os.path.join(dst, "meta.json")) as fh:
        return json.load(fh)


def build(args):
    today = args.today or dt.datetime.now(dt.timezone.utc).date().isoformat()
    prev_meta, prev_matches = read_prev(args.prev)
    tmp = os.path.join(args.out, ".variants.json.gz")
    os.makedirs(args.out, exist_ok=True)
    same_format = prev_meta.get("format") == FORMAT     # a new file format is always rebuilt, even from the same export
    status, etag = download_export(args.export, prev_meta.get("etag") if same_format and not args.force else None, tmp)
    if status == "unchanged" and carry_forward(args.prev, args.out):
        log("export unchanged (ETag); previous combo files kept")
        return "unchanged"
    log(f"export downloaded: {os.path.getsize(tmp) / 1e6:.1f} MB")
    timestamp, version, variants, aliases = read_export(tmp)
    if (not args.force and timestamp and timestamp == prev_meta.get("timestamp")
            and prev_meta.get("format") == FORMAT and carry_forward(args.prev, args.out)):
        log(f"export timestamp {timestamp} unchanged; previous combo files kept")
        return "unchanged"

    t0 = time.time()
    formats = {}
    count, cards, templates, features, recipes, full, short, uses_by_name, sizes = split_variants(variants(), formats)
    al = aliases()
    log(f"{count} variants, {len(cards)} cards, {len(templates)} templates, {len(features)} features, "
        f"{len(recipes)} recipes, {len(al)} aliases ({time.time() - t0:.0f}s)")
    if count < args.min_variants:
        raise RuntimeError(f"only {count} variants in the export; refusing to publish")

    matches = match_templates(templates, prev_matches, today, args.match_days, args.match_budget)
    fits = defaultdict(set)
    for tid, m in matches.items():
        for name in m.get("m") or []:
            for nm in names_for(name):
                fits[nm].add(int(tid) if str(tid).isdigit() else tid)
    card_of = {}
    for k, c in cards.items():
        for nm in names_for(c.get("name") or ""):
            card_of.setdefault(nm, int(k) if k.isdigit() else k)

    root = os.path.join(args.out, "combo")
    shutil.rmtree(root, ignore_errors=True)
    files, total = {}, {"v": 0, "i": 0}
    for b, vs in full.items():
        total["v"] += dump(os.path.join(root, "v", b + ".json"), vs)
    index = defaultdict(dict)
    for nm in set(uses_by_name) | set(fits):
        e = {}
        if nm in card_of:
            e["c"] = card_of[nm]
        if nm in uses_by_name:
            e["v"] = [short[i] for i in sorted(uses_by_name[nm])]
        if nm in fits:
            e["t"] = sorted(fits[nm])
        index[prefix_of(nm)][nm] = e
    by_template = defaultdict(list)
    for vid, r in short.items():
        for t, _q in r[2]:
            by_template[str(t)].append(r)
    tfiles = {}
    for t, recs in by_template.items():
        path = os.path.join(root, "tv", t + ".json")
        total["i"] += dump(path, sorted(recs, key=lambda r: r[0]))
        with open(path, "rb") as fh:
            tfiles[t] = hashlib.sha1(fh.read()).hexdigest()[:10]
    for p, entries in index.items():
        path = os.path.join(root, "i", p + ".json")
        total["i"] += dump(path, entries)
        with open(path, "rb") as fh:
            files[p] = hashlib.sha1(fh.read()).hexdigest()[:10]
    names = {k: c.get("name") for k, c in cards.items()}
    side = {name: dump(os.path.join(root, name + ".json"), obj) for name, obj in
            (("names", names),)}
    with open(os.path.join(root, "names.json"), "rb") as fh:
        names_hash = hashlib.sha1(fh.read()).hexdigest()[:10]
    side.update({name: dump(os.path.join(root, name + ".json"), obj) for name, obj in
            (("cards", cards), ("templates", templates), ("features", features), ("recipes", recipes), ("aliases", al),
             ("matches", matches))})

    meta = {"format": FORMAT, "timestamp": timestamp, "version": version, "etag": etag, "built": today,
            "variants": count, "aliases": len(al), "cards": len(cards), "templates": len(templates),
            "features": len(features), "combos": len(recipes), "buckets": len(full), "indexFiles": len(index),
            "index": files, "tindex": tfiles, "names": names_hash, "formats": sorted(formats),
            "templateMatches": len(matches), "matchedOn": min((m.get("d") for m in matches.values()), default=None),
            "source": "https://commanderspellbook.com/", "credit": "Combo data from Commander Spellbook"}
    dump(os.path.join(root, "meta.json"), meta)
    log(f"written: variants {total['v'] / 1e6:.1f} MB in {len(full)} files, index {total['i'] / 1e6:.1f} MB in {len(index)} files, "
        + ", ".join(f"{k} {v / 1e6:.2f} MB" for k, v in side.items()))
    log("variant field sizes (MB): " + ", ".join(f"{k} {v / 1e6:.1f}" for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])))
    os.remove(tmp)
    return "built"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export", default=EXPORT, help="URL or local path of variants.json(.gz)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev", help="the previous run's published files (for the ETag, timestamp and template matches)")
    ap.add_argument("--match-days", type=int, default=7, help="search a template's cards again after this many days")
    ap.add_argument("--match-budget", type=int, default=1800, help="seconds allowed for template searches per run")
    ap.add_argument("--min-variants", type=int, default=1000)
    ap.add_argument("--force", action="store_true", help="rebuild even if the export did not change")
    ap.add_argument("--today")
    args = ap.parse_args()
    try:
        print(build(args))
    except Exception as e:
        log(f"build failed ({e!r})")
        if carry_forward(args.prev, args.out):
            log("previous combo files kept")
            print("kept")
        else:
            print("failed")
            sys.exit(1)


if __name__ == "__main__":
    main()
