"""One-off (Nov 2026): for each commander (and theme) in scan/jobs2.json, EDHREC's card stats, theme cards and average deck, in the
test-set format of the app's strategy check (tools/strategy-check). Writes scan/decks2.json."""
import json, time, urllib.request, re
BASE = "https://json.edhrec.com/pages/"
UA = {"User-Agent": "DeckShelf-scan/1.0"}
def get(path, tries=3):
    for t in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=UA), timeout=30) as r: return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(3 * (t + 1))
        except Exception as e:
            print("retry", path, e, flush=True); time.sleep(3 * (t + 1))
    return None
def cardstats(d):
    out = {}
    for cl in ((d or {}).get("container", {}).get("json_dict", {}).get("cardlists") or []):
        for cv in cl.get("cardviews", []):
            if cv.get("name") and cv.get("potential_decks"): out[cv["name"]] = [cv.get("num_decks"), cv.get("potential_decks"), cv.get("synergy")]
    return out
def avgdeck(d):
    # the average deck page: a "deck" list of "1 Card Name" lines, or card lists by type
    if not d: return None
    lines = d.get("deck") or (d.get("container", {}).get("json_dict", {}) or {}).get("deck")
    cards = []
    if isinstance(lines, list) and lines and isinstance(lines[0], str):
        for l in lines:
            m = re.match(r"\s*(\d+)\s+(.+)", l)
            if m: cards.append([m.group(2).strip(), int(m.group(1))])
    else:
        for cl in ((d.get("container", {}).get("json_dict", {}) or {}).get("cardlists") or []):
            for cv in cl.get("cardviews", []): cards.append([cv.get("name"), 1])
    return {"cards": cards, "keys": list(d.keys())[:12]}
jobs = json.load(open("scan/jobs2.json")); out = []
for k, j in enumerate(jobs):
    s, th = j["slug"], j["theme"]
    page = get(f"commanders/{s}.json"); time.sleep(0.3)
    tpage = get(f"commanders/{s}/{th}.json") if th else None; time.sleep(0.3)
    avg = avgdeck(get(f"average-decks/{s}/{th}.json" if th else f"average-decks/{s}.json")); time.sleep(0.3)
    rec = dict(j, num_decks=(page or {}).get("num_decks_avg"), themes=((page or {}).get("panels", {}) or {}).get("taglinks"),
               base=cardstats(page), theme_cards=cardstats(tpage), theme_count=(tpage or {}).get("num_decks_avg"), avg=avg)
    out.append(rec)
    print(k, j["name"], th, len(rec["base"]), len((avg or {}).get("cards") or []), flush=True)
json.dump(out, open("scan/decks2.json", "w"))
print("done", len(out))
