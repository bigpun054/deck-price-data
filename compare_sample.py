#!/usr/bin/env python3
"""Fetch Scryfall's own objects for a sample of printings/tokens listed in the data branch (checked out in ./prev) and
HEAD-check the picture addresses the app derives from the ids. Output goes to out/ for a local comparison."""
import json, os, random, sys, time, urllib.request, glob

UA = {"User-Agent": "deck-shelf-compare/1.0", "Accept": "application/json", "Content-Type": "application/json"}
random.seed(7)
TRICKY = ["Delver of Secrets", "Fire // Ice", "Bonecrusher Giant", "Brazen Borrower", "Valki, God of Lies", "Tergrid's Shadow",
          "Elspeth, Sun's Champion", "Sol Ring", "Lightning Bolt", "Goblin Guide", "Forest", "Command Tower", "Esika's Chariot",
          "Jace, Vryn's Prodigy", "Akroma's Memorial", "Urza, Lord Protector", "Bruna, the Fading Light", "Gisela, the Broken Blade",
          "Archangel Avacyn", "Huntmaster of the Fells", "Budoka Gardener", "Cut // Ruin", "Wear // Tear", "Realm-Cloaked Giant",
          "Ajani, Nacatl Pariah", "Invasion of Zendikar", "Sheoldred, the Apocalypse", "The One Ring", "Atraxa, Praetors' Voice",
          "Rite of Flame", "Dryad Arbor", "Lim-Dul's Vault", "Chittering Rats", "Hostage Taker", "Rhystic Study", "Smothering Tithe"]


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), UA)
    for a in range(3):
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except Exception as e:
            print("retry", e, flush=True); time.sleep(2 + a * 3)
    return {"data": [], "not_found": []}


def head(url):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA["User-Agent"]}), timeout=30)
        return r.status
    except Exception as e:
        return getattr(e, "code", str(e))


def norm(s):
    import re
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


names = {}
for f in glob.glob("prev/n/*.json"):
    names.update(json.load(open(f)))
allp = [(k, p["i"]) for k, e in names.items() for p in e["p"]]
ids = {i for k, e in names.items() if k in {norm(t) for t in TRICKY} for i in (p["i"] for p in e["p"])}
ids |= {i for _, i in random.sample(allp, 700)}
ids = sorted(ids)
toks = []
for f in glob.glob("prev/t/*.json"):
    toks += [t["i"] for t in json.load(open(f)).values()]
tokids = random.sample(toks, 150)
os.makedirs("out", exist_ok=True)
for label, lst in (("cards", ids), ("tokens", tokids)):
    got, miss = [], []
    for i in range(0, len(lst), 75):
        r = post("https://api.scryfall.com/cards/collection", {"identifiers": [{"id": x} for x in lst[i:i + 75]]})
        got += r.get("data", []); miss += r.get("not_found", [])
        time.sleep(0.12)
    json.dump({"data": got, "not_found": miss}, open(f"out/scry_{label}.json", "w"), separators=(",", ":"))
    print(label, len(lst), "asked,", len(got), "got,", len(miss), "not found", flush=True)
# picture addresses
pics = {}
sample = random.sample(ids, 120) + random.sample(tokids, 20)
for i in sample:
    base = f"https://cards.scryfall.io/%s/%s/{i[0]}/{i[1]}/{i}.%s"
    pics[i] = {"front": head(base % ("normal", "front", "jpg")), "back": head(base % ("normal", "back", "jpg")),
               "png": head(base % ("png", "front", "png")), "art": head(base % ("art_crop", "front", "jpg"))}
json.dump(pics, open("out/pics.json", "w"))
from collections import Counter
print("front", Counter(v["front"] for v in pics.values()), "png", Counter(v["png"] for v in pics.values()), "art", Counter(v["art"] for v in pics.values()))
