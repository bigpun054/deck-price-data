"""Temporary: builds a test set from EDHREC for tuning Deck Shelf's strategy guess.
For each commander: its themes with deck counts, its card stats, and for its top 3 themes (50+ decks) that theme's card stats
and average deck. Output: testset/<slug>.json (compact)."""
import json, os, time, urllib.request, urllib.error
BASE = "https://json.edhrec.com/pages/"
def get(path):
    for t in range(3):
        try:
            req = urllib.request.Request(BASE + path + ".json", headers={"User-Agent": "deck-shelf-test"})
            with urllib.request.urlopen(req, timeout=30) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404): return None
        except Exception: pass
        time.sleep(2 + t * 3)
    return None
def stats(j):
    out = {}
    for cl in (j or {}).get("container", {}).get("json_dict", {}).get("cardlists", []):
        for c in cl.get("cardviews", []):
            if c.get("name") and c["name"] not in out: out[c["name"]] = [c.get("num_decks"), c.get("potential_decks"), round(c.get("synergy") or 0, 3)]
    return out
top = get("commanders/year")
slugs = [c["sanitized"] for c in top["container"]["json_dict"]["cardlists"][0]["cardviews"]]
mine = ["radagast-of-rhosgobel", "lorehold-the-historian", "marneus-calgar", "ultron-artificial-malevolence", "zimone-infinite-analyst",
        "doran-besieged-by-time", "thrasios-triton-hero-tymna-the-weaver", "tymna-the-weaver-thrasios-triton-hero", "jin-sakai-ghost-of-tsushima"]
os.makedirs("testset", exist_ok=True)
log = []
for slug in mine + [s for s in slugs if s not in mine]:
    j = get("commanders/" + slug); time.sleep(0.3)
    if not j: log.append(f"{slug}: no page"); continue
    themes = [{"slug": t["slug"], "value": t["value"], "count": t["count"]} for t in (j.get("panels", {}).get("taglinks") or [])]
    card = j.get("container", {}).get("json_dict", {}).get("card", {})
    rec = {"slug": slug, "name": card.get("name"), "num_decks": card.get("num_decks"), "themes": themes[:30], "base": stats(j), "avg": None, "theme": {}}
    a = get("average-decks/" + slug); time.sleep(0.3)
    if a and a.get("deck"): rec["avg"] = {"cmd": a["deck"].get("commander"), "cards": a["deck"].get("cards")}
    for t in [t for t in themes if t["count"] >= 50][:3]:
        tp = get(f"commanders/{slug}/{t['slug']}"); time.sleep(0.3)
        av = get(f"average-decks/{slug}/{t['slug']}"); time.sleep(0.3)
        rec["theme"][t["slug"]] = {"count": t["count"], "cards": stats(tp), "avg": {"cmd": av["deck"].get("commander"), "cards": av["deck"].get("cards")} if av and av.get("deck") else None}
    json.dump(rec, open(f"testset/{slug}.json", "w"), separators=(",", ":"))
    log.append(f"{slug}: {len(themes)} themes, {len(rec['theme'])} theme files, avg {'yes' if rec['avg'] else 'no'}")
open("testset/log.txt", "w").write("\n".join(log)); print("\n".join(log))
