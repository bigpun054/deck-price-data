"""One-off scan (Nov 2026): EDHREC's top commanders of the past year, each with its themes and deck count.
Writes scan/commanders.json. Run by .github/workflows/edhrec-scan.yml on the edhrec-scan branch."""
import json, time, urllib.request, sys
BASE = "https://json.edhrec.com/pages/"
UA = {"User-Agent": "DeckShelf-scan/1.0"}
WANT = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
def get(path, tries=4):
    for t in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            print("retry", path, e, flush=True); time.sleep(3 * (t + 1))
    return None
def lists(d):
    c = (d or {}).get("container", {}).get("json_dict", {})
    return c.get("cardlists") or []
out, seen, path, log = [], set(), "commanders/year.json", []
while path and len(out) < WANT:
    d = get(path); log.append([path, bool(d)])
    if not d: break
    nxt = None
    for cl in lists(d):
        for cv in cl.get("cardviews", []):
            s = cv.get("sanitized") or (cv.get("url") or "").split("/")[-1]
            if s and s not in seen:
                seen.add(s); out.append({"name": cv.get("name"), "slug": s, "decks": cv.get("num_decks") or cv.get("inclusion")})
        nxt = cl.get("more") or nxt
    if not nxt: nxt = d.get("more")
    path = nxt if nxt and nxt != path else None
    time.sleep(0.4)
print("list:", len(out), log[-3:], flush=True)
for k, c in enumerate(out[:WANT]):
    d = get(f"commanders/{c['slug']}.json")
    if d:
        c["themes"] = [[t.get("slug"), t.get("count")] for t in (d.get("panels", {}).get("taglinks") or [])]
        c["num"] = d.get("num_decks_avg") or (d.get("container", {}).get("json_dict", {}).get("card", {}) or {}).get("num_decks")
    if k % 50 == 0: print(k, c["name"], len(c.get("themes") or []), flush=True)
    time.sleep(0.35)
json.dump({"at": time.strftime("%Y-%m-%d"), "log": log, "commanders": out[:WANT]}, open("scan/commanders.json", "w"), indent=0)
print("done", len(out[:WANT]))
