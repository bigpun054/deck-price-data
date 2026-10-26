#!/usr/bin/env python3
"""One-off probe: print MTGJSON's CSV columns and a few sample rows so the card-detail builder uses real column names."""
import csv, io, sys
from build_history import open_any

csv.field_size_limit(1 << 30)
BASE = "https://mtgjson.com/api/v5/csv/"
WANT = {"Delver of Secrets", "Sol Ring", "Elspeth, Sun's Champion", "Fire // Ice", "Fire", "Bonecrusher Giant",
        "Brazen Borrower", "Valki, God of Lies", "Tergrid's Shadow", "Lightning Bolt", "Goblin Guide"}


def rows(name):
    f = open_any(BASE + name)
    return csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline=""))


def show(row, cap=160):
    return {k: (v or "")[:cap] for k, v in row.items() if v not in (None, "")}


for fname in ["cards.csv", "tokens.csv", "sets.csv", "cardIdentifiers.csv", "tokenIdentifiers.csv"]:
    print(f"\n===== {fname}", flush=True)
    try:
        r = rows(fname)
        print("COLUMNS:", list(r.fieldnames or []), flush=True)
        n, shown, kinds = 0, 0, {}
        for row in r:
            n += 1
            nm = row.get("name") or row.get("code") or ""
            if fname == "cards.csv":
                if nm in WANT or (row.get("name") or "").split(" // ")[0] in WANT:
                    if shown < 14:
                        print("ROW:", show(row), flush=True); shown += 1
                kinds[row.get("layout", "")] = kinds.get(row.get("layout", ""), 0) + 1
            elif fname == "tokens.csv":
                if shown < 6 and (row.get("name") == "Soldier" or "Elspeth, Sun's Champion" in (row.get("reverseRelated") or "")):
                    print("ROW:", show(row), flush=True); shown += 1
                kinds[(row.get("setCode") or "")[:1]] = kinds.get((row.get("setCode") or "")[:1], 0) + 1
            elif fname == "sets.csv":
                if row.get("code") in ("MKC", "TMKC", "MH2", "TMH2", "SNC", "TSNC", "SLD", "PLST"):
                    print("ROW:", show(row), flush=True)
            elif fname in ("cardIdentifiers.csv", "tokenIdentifiers.csv"):
                if shown < 4:
                    print("ROW:", show(row), flush=True); shown += 1
        print("ROWS:", n, "KINDS:", dict(sorted(kinds.items(), key=lambda x: -x[1])[:25]), flush=True)
    except Exception as e:
        print("FAILED:", fname, e, flush=True)
