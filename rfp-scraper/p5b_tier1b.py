"""Phase 5b - Tier 1, retargeted.

Why this replaces the first Tier 1 design: that version spent 14 page loads per
agency re-running the ladder Tier 0 had already run, and converted only ~6 of 160
agencies in ~50 minutes. The measured gap was different:

  67 of 190 agencies WAF-blocked our static client, and
  47 of those served NO sitemap to it either
  -- while sitemap.xml is the single best signal (58% resolution when it has a hit).

So the browser's job is not to re-probe paths. It is to fetch the two documents a
WAF denied us -- **sitemap.xml** and the homepage -- as a real browser, and then
feed them into the same ranking + VERIFY code. ~5 page loads per agency.

Sessions w1..w4, `--raw` only, textContent for link text, model reads nothing.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from urllib.parse import urljoin, urlparse

import common as C
from p5_tier1 import (SESSIONS, LINKS_JS, HTML_JS, cli, raw_json, goto, SETTLE,
                      ACCEPT, ESCALATE, rank_links, _print_lock)
from p3_tier0 import path_rank

MAX_GOTO = 7
# XML in a browser: Chromium renders a tree, so read the raw text, not the DOM.
XML_JS = """JSON.stringify({u:location.href,
 t:(document.body?document.body.innerText:'').slice(0,600000)})"""

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml"]


def sitemap_candidates(session, base, gotos, budget):
    """Pull sitemap.xml through the browser and return lexicon-matching URLs."""
    locs, used = [], 0
    for p in SITEMAP_PATHS:
        if gotos + used >= budget:
            break
        used += 1
        if not goto(session, urljoin(base, p)):
            continue
        time.sleep(0.4)
        d = raw_json(session, XML_JS)
        if not d or not d.get("t"):
            continue
        txt = d["t"]
        found = re.findall(r"https?://[^\s<>\"']+", txt)
        if not found:
            continue
        # a sitemap index points at child sitemaps; follow one generation
        children = [u for u in found if re.search(r"sitemap.*\.xml", u, re.I)]
        if children and len(found) - len(children) < 5:
            for c in children[:2]:
                if gotos + used >= budget:
                    break
                used += 1
                if not goto(session, c):
                    continue
                time.sleep(0.4)
                d2 = raw_json(session, XML_JS)
                if d2 and d2.get("t"):
                    locs += re.findall(r"https?://[^\s<>\"']+", d2["t"])
        else:
            locs += found
        if locs:
            break
    return locs, used


def process(agency, session, cands):
    site = agency["agency_website"] or ("https://" + agency["agency_domain"])
    gotos = 0
    best = None

    def record(url, signal, score, ev, meta, link_text=""):
        nonlocal best
        cands.append({
            "agency_name": agency["agency_name"], "agency_domain": agency["agency_domain"],
            "url": url, "tier": "1b", "signal": signal, "score": score,
            "evidence": "|".join(ev) + f"|items={meta.get('items')}",
            "decision": "accepted" if score >= ACCEPT else "rejected",
            "note": (meta.get("title") or "")[:120]})
        cand = {"url": url, "signal": signal, "score": score, "ev": ev, "meta": meta}
        if best is None or (score, -C.slug_specificity(url), C.depth(url)) > \
                (best["score"], -C.slug_specificity(best["url"]), C.depth(best["url"])):
            best = cand

    def visit(url, signal, link_text=""):
        nonlocal gotos
        if gotos >= MAX_GOTO:
            return -1
        gotos += 1
        if not goto(session, url):
            return -1
        time.sleep(SETTLE)
        d = raw_json(session, HTML_JS)
        if not d or not d.get("h"):
            return -1
        s, ev, meta = C.verify_score(d["u"], d["h"], link_text)
        meta["html"] = d["h"]
        record(d["u"], signal, s, ev, meta, link_text)
        return s

    # ---- 1. sitemap through the browser (the signal the WAF denied us) ----
    locs, used = sitemap_candidates(session, site, gotos, MAX_GOTO - 3)
    gotos += used
    sm = [u for u in set(locs)
          if C.PATH_LEX.search(urlparse(u).path)
          and not C.NEG_LEX.search(urlparse(u).path)
          and C.same_org(u, agency["agency_domain"], site)]
    sm.sort(key=path_rank, reverse=True)
    for u in sm[:3]:
        visit(u, "browser-sitemap")
        if best and best["score"] >= ACCEPT and best["meta"].get("items", 0) >= 1:
            break

    # ---- 2. rendered homepage links (only if the sitemap route failed) ----
    home, blocked = None, ""
    if not best or best["score"] < ACCEPT:
        gotos += 1
        if goto(session, site):
            time.sleep(SETTLE)
            home = raw_json(session, LINKS_JS)
        # A6: confirm a WAF block with a real browser, then document it - the
        # instruction is explicit that a block gets recorded, never circumvented.
        if home and (re.search(r"(403|forbidden|attention required|access denied"
                               r"|just a moment|blocked|permission to access)",
                               home.get("t", ""), re.I)
                     or home.get("n", 0) == 0):
            blocked = (f"blocked: real Chrome also received "
                       f"\"{(home.get('t') or 'empty page').strip()[:60]}\" "
                       f"- not circumvented (A6)")
            home = None
        if home:
            for s, t, h in rank_links(home.get("L", []), agency, site)[:3]:
                visit(h, "browser-link", t)
                if best and best["score"] >= ACCEPT:
                    break

    status = "unresolved"
    url = ""
    if best and best["score"] >= ACCEPT:
        status, url = "resolved", best["url"]
    elif best and best["score"] >= ESCALATE:
        status = "escalate"
    return {"idx": agency["idx"], "agency_name": agency["agency_name"], "tier": 1,
            "status": status, "rfp_url": url,
            "score": best["score"] if best else None,
            "signal": best["signal"] if best else "",
            "evidence": "|".join(best["ev"]) if best else "",
            "items": best["meta"].get("items") if best else None,
            "empty_state": best["meta"].get("empty_state") if best else None,
            "cms": "", "via": "", "best_alt": [], "gotos": gotos,
            "notes_hint": blocked, "home_ok": bool(home) or bool(locs),
            "not_found_reason": blocked, "browser_blocked": bool(blocked),
            "sitemap_locs": len(locs)}


def worker(session, slice_, results):
    cli(session, "open", "--browser=chrome", timeout=120)
    with _print_lock:
        print(f"  [{session}] {len(slice_)} agencies", flush=True)
    for i, a in enumerate(slice_, 1):
        cands = []
        try:
            rec = process(a, session, cands)
        except Exception as e:
            rec = {"idx": a["idx"], "agency_name": a["agency_name"], "tier": 1,
                   "status": "unresolved", "rfp_url": "", "score": None, "signal": "",
                   "evidence": "", "items": None, "empty_state": None, "cms": "",
                   "via": "", "best_alt": [], "gotos": 0, "home_ok": False,
                   "notes_hint": f"tier1b error {type(e).__name__}"}
        with _print_lock:
            C.log_candidates(cands)
            C.checkpoint_write(rec)
            results.append(rec)
            if i % 10 == 0:
                print(f"  [{session}] {i}/{len(slice_)} "
                      f"(resolved {sum(1 for r in results if r['status']=='resolved')})",
                      flush=True)
    cli(session, "close", timeout=60)


def main():
    agencies = {a["idx"]: a for a in C.load_agencies() if a["lane"] == "main"}
    done = C.checkpoint_read()
    todo = [a for i, a in agencies.items()
            if (done.get(str(i)) or {}).get("status") not in ("resolved", "identity-mismatch")]
    if "--limit" in sys.argv:
        todo = todo[:int(sys.argv[sys.argv.index("--limit") + 1])]
    if "--only" in sys.argv:
        ids = set(sys.argv[sys.argv.index("--only") + 1].split(","))
        todo = [a for a in agencies.values() if str(a["idx"]) in ids]
    print(f"tier1b: {len(todo)} agencies, {len(SESSIONS)} sessions, "
          f"<={MAX_GOTO} page loads each")
    slices = [todo[i::len(SESSIONS)] for i in range(len(SESSIONS))]
    results, threads = [], []
    t0 = time.time()
    for s, sl in zip(SESSIONS, slices):
        if not sl:
            continue
        th = threading.Thread(target=worker, args=(s, sl, results))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    cli("w1", "close-all", timeout=60)
    ok = [r for r in results if r["status"] == "resolved"]
    print(f"\nTIER 1b RESULT  processed={len(results)}  resolved={len(ok)}  "
          f"({round(time.time()-t0)}s)")
    sig = {}
    for r in ok:
        sig[r["signal"]] = sig.get(r["signal"], 0) + 1
    print("by signal:", sig)
    print("sitemaps recovered via browser:",
          sum(1 for r in results if (r.get("sitemap_locs") or 0) > 0))


if __name__ == "__main__":
    main()
