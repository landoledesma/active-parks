"""Phase 6 - rendered re-verification of near-miss candidates.

Tier 0 scores static HTML. The dominant false negative is a *correct* page whose
listing is injected by JavaScript, so it scores 20-59 and is discarded. This pass
re-opens every near-miss candidate in a real browser and re-runs the identical
VERIFY scorer against the rendered DOM.

Also implements Tier 2: when a candidate is a known SPA portal that renders an
empty DOM, the XHR responses are inspected to confirm the listing API returns
solicitations (`requests` + `request <n>`), which DOM scraping cannot see.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import threading
import time
from collections import defaultdict

import common as C
from p5_tier1 import (SESSIONS, HTML_JS, cli, raw_json, goto, SETTLE,
                      ACCEPT, ESCALATE, _print_lock)

MIN_SCORE = 10          # only revisit candidates with some positive signal
MAX_PER_AGENCY = 4


def load_near_misses(unresolved_idx, agencies_by_name):
    """Candidates worth a rendered second look, grouped per agency."""
    by_agency = defaultdict(list)
    try:
        rows = list(csv.DictReader(open(C.DEBUG_CSV, encoding="utf-8")))
    except FileNotFoundError:
        return {}
    for r in rows:
        try:
            sc = int(r["score"])
        except (ValueError, TypeError):
            continue
        a = agencies_by_name.get(r["agency_name"])
        if not a or a["idx"] not in unresolved_idx:
            continue
        if not (MIN_SCORE <= sc < ACCEPT):
            continue
        by_agency[a["idx"]].append((sc, r["url"], r["signal"], r["note"]))
    out = {}
    for idx, lst in by_agency.items():
        seen, ded = set(), []
        for sc, u, sig, note in sorted(lst, key=lambda x: -x[0]):
            n = C.norm_url(u)
            if n in seen:
                continue
            seen.add(n)
            ded.append((sc, u, sig, note))
        out[idx] = ded[:MAX_PER_AGENCY]
    return out


def portal_has_items(session):
    """Tier 2: confirm a SPA portal's XHR listing actually returns solicitations."""
    rc, out, err = cli(session, "--raw", "requests", timeout=60)
    if rc != 0 or not out:
        return False, ""
    idxs = re.findall(r"^\s*\[?(\d+)\]?\s", out, re.M)[:40]
    hits = 0
    for n in idxs[-25:]:
        rc2, body, _ = cli(session, "--raw", "request", n, timeout=45)
        if rc2 != 0 or not body:
            continue
        if len(body) < 40:
            continue
        if re.search(r"(bid|solicitation|rfp|proposal|opportunit)", body, re.I) and \
           re.search(r"(close|due|deadline|status|posted|open)", body, re.I):
            hits += 1
            if hits >= 1:
                return True, f"XHR #{n} returns solicitation records"
    return False, ""


def worker(session, slice_, near, agencies, results):
    cli(session, "open", "--browser=chrome", timeout=120)
    with _print_lock:
        print(f"  [{session}] {len(slice_)} agencies", flush=True)
    for i, idx in enumerate(slice_, 1):
        a = agencies[idx]
        cands, best = [], None
        for sc, url, sig, note in near.get(idx, []):
            if not goto(session, url):
                continue
            time.sleep(SETTLE)
            d = raw_json(session, HTML_JS)
            if not d or not d.get("h"):
                continue
            s, ev, meta = C.verify_score(d["u"], d["h"])
            extra = ""
            # Tier 2: portal with an empty-looking DOM -> check the XHR layer
            if s < ACCEPT and meta.get("items", 0) == 0 and C.portal_of(d["u"]):
                ok, why = portal_has_items(session)
                if ok:
                    s += 25
                    ev.append("xhr-items+25")
                    extra = why
            cands.append({
                "agency_name": a["agency_name"], "agency_domain": a["agency_domain"],
                "url": d["u"], "tier": "2", "signal": f"reverify:{sig}", "score": s,
                "evidence": "|".join(ev) + f"|items={meta.get('items')}",
                "decision": "accepted" if s >= ACCEPT else "rejected",
                "note": (meta.get("title") or "")[:120]})
            if best is None or (s, C.depth(d["u"])) > (best[0], C.depth(best[1])):
                best = (s, d["u"], f"reverify:{sig}", ev, meta, extra)
        rec = None
        if best and best[0] >= ACCEPT:
            rec = {"idx": idx, "agency_name": a["agency_name"], "tier": 2,
                   "status": "resolved", "rfp_url": best[1], "score": best[0],
                   "signal": best[2], "evidence": "|".join(best[3]),
                   "items": best[4].get("items"),
                   "empty_state": best[4].get("empty_state"), "cms": "", "via": "",
                   "best_alt": [], "notes_hint": best[5], "home_ok": True}
        with _print_lock:
            C.log_candidates(cands)
            if rec:
                C.checkpoint_write(rec)
                results.append(rec)
            if i % 10 == 0:
                print(f"  [{session}] {i}/{len(slice_)}", flush=True)
    cli(session, "close", timeout=60)


def main():
    agencies = {a["idx"]: a for a in C.load_agencies() if a["lane"] == "main"}
    by_name = {a["agency_name"]: a for a in agencies.values()}
    done = C.checkpoint_read()
    unresolved = {i for i in agencies
                  if (done.get(str(i)) or {}).get("status") != "resolved"}
    near = load_near_misses(unresolved, by_name)
    todo = sorted(near.keys())
    if "--limit" in sys.argv:
        todo = todo[:int(sys.argv[sys.argv.index("--limit") + 1])]
    print(f"reverify: {len(todo)} agencies with near-miss candidates "
          f"({sum(len(v) for v in near.values())} URLs)")

    slices = [todo[i::len(SESSIONS)] for i in range(len(SESSIONS))]
    results, threads = [], []
    t0 = time.time()
    for s, sl in zip(SESSIONS, slices):
        if not sl:
            continue
        th = threading.Thread(target=worker, args=(s, sl, near, agencies, results))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    cli("w1", "close-all", timeout=60)
    print(f"\nREVERIFY RESULT  newly resolved={len(results)}  ({round(time.time()-t0)}s)")
    for r in sorted(results, key=lambda x: x["idx"])[:40]:
        print(f"   {r['idx']:3} {r['score']:>3} {r['agency_name'][:28]:30} {r['rfp_url'][:80]}")


if __name__ == "__main__":
    main()
