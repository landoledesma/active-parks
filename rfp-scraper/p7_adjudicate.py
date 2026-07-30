"""Phase 7 - final adjudication. Authoritative VERIFY over every candidate.

The scorer improved while the sweep ran (PLAN.md R3.1-R3.3, and the specific-slug
rule). Rather than trust scores computed by older revisions, this pass re-fetches
EVERY candidate any tier ever proposed and re-scores it with the current scorer:

  stage 1  static re-fetch + re-score (all candidates >= MIN_SCORE)
  stage 2  rendered re-check in a real browser for agencies still unresolved
           whose best static candidate lands in the 30-59 escalate band

Output is out/final.jsonl, which phase 9 treats as authoritative (the checkpoint
merge deliberately keeps the *best* record, so a demotion could not be expressed
there). Demotions are the point: a wrong URL costs more than a blank one.
"""
from __future__ import annotations

import asyncio
import csv
import json
import re
import sys
import threading
import time
from collections import defaultdict

import httpx

import common as C
from p3_tier0 import HEADERS, links_of

MIN_SCORE = 20
ACCEPT, ESCALATE = 60, 30
MAX_PER_AGENCY = 6
FINAL = C.OUT + "/final.jsonl"


def better(a, b):
    """Prefer score, then a listing-shaped slug, then the deeper (more specific) URL."""
    if a is None:
        return b
    if b is None:
        return a
    ka = (a["score"], -C.slug_specificity(a["url"]), C.depth(a["url"]))
    kb = (b["score"], -C.slug_specificity(b["url"]), C.depth(b["url"]))
    return a if ka >= kb else b


def gather(agencies_by_name, done):
    by_idx = defaultdict(list)
    rows = list(csv.DictReader(open(C.DEBUG_CSV, encoding="utf-8")))
    for r in rows:
        try:
            sc = int(r["score"])
        except (ValueError, TypeError):
            continue
        a = agencies_by_name.get(r["agency_name"])
        if not a or sc < MIN_SCORE:
            continue
        by_idx[a["idx"]].append((sc, r["url"], r["signal"]))
    # always include whatever the pipeline currently emits
    for k, rec in done.items():
        if rec.get("rfp_url"):
            by_idx[int(k)].append((rec.get("score") or 0, rec["rfp_url"],
                                   rec.get("signal", "")))
    out = {}
    for idx, lst in by_idx.items():
        seen, ded = set(), []
        for sc, u, sig in sorted(lst, key=lambda x: -x[0]):
            n = C.norm_url(u)
            if n in seen:
                continue
            seen.add(n)
            ded.append((sc, u, sig))
        out[idx] = ded[:MAX_PER_AGENCY]
    return out


async def adjudicate(idx, agency, cands, sem, c1, c2, results, logrows, fetched):
    async with sem:
        best = None
        for old, url, sig in cands:
            r = None
            for cl in (c1, c2):
                try:
                    r = await cl.get(url, headers=HEADERS, follow_redirects=True,
                                     timeout=20.0)
                    break
                except Exception:
                    continue
            if r is None or r.status_code != 200:
                code = r.status_code if r is not None else "ERR"
                logrows.append({"agency_name": agency["agency_name"],
                                "agency_domain": agency["agency_domain"], "url": url,
                                "tier": "7", "signal": f"adjudicate:{sig}", "score": -1,
                                "evidence": f"http {code}",
                                "decision": "inconclusive", "note": ""})
                fetched.setdefault(idx, {})[C.norm_url(url)] = f"http {code}"
                continue
            fetched.setdefault(idx, {})[C.norm_url(url)] = "ok"
            if "html" not in r.headers.get("content-type", ""):
                continue
            html = r.text
            s, ev, meta = C.verify_score(str(r.url), html)
            logrows.append({
                "agency_name": agency["agency_name"], "agency_domain": agency["agency_domain"],
                "url": str(r.url), "tier": "7", "signal": f"adjudicate:{sig}", "score": s,
                "evidence": "|".join(ev) + f"|items={meta.get('items')}|was={old}",
                "decision": "accepted" if s >= ACCEPT else "rejected",
                "note": (meta.get("title") or "")[:120]})
            cand = {"url": str(r.url), "score": s, "signal": sig, "ev": ev,
                    "items": meta.get("items"), "empty_state": meta.get("empty_state"),
                    "html": html}
            best = better(best, cand)
        results[idx] = best


async def stage1():
    agencies = {a["idx"]: a for a in C.load_agencies()}
    by_name = {a["agency_name"]: a for a in agencies.values()}
    done = C.checkpoint_read()
    cands = gather(by_name, done)
    print(f"stage 1: re-verifying {sum(len(v) for v in cands.values())} candidate URLs "
          f"across {len(cands)} agencies")
    # 8, not 16: the first run rate-limited *itself* into a 429 on nputnam.k12.in.us
    # and then treated its own 429 as evidence the page was bad.
    sem = asyncio.Semaphore(8)
    results, logrows, fetched = {}, [], {}
    async with httpx.AsyncClient(verify=True) as c1, \
               httpx.AsyncClient(verify=False) as c2:
        await asyncio.gather(*[
            adjudicate(idx, agencies[idx], lst, sem, c1, c2, results, logrows, fetched)
            for idx, lst in cands.items()])
    C.log_candidates(logrows)
    return agencies, results, done, fetched


def stage2(agencies, results, done):
    """Rendered re-check for agencies stuck in the escalate band."""
    from p5_tier1 import SESSIONS, HTML_JS, cli, raw_json, goto, SETTLE, _print_lock
    todo = [idx for idx, b in results.items()
            if b is not None and ESCALATE <= b["score"] < ACCEPT]
    # Also re-open anything an earlier tier had RESOLVED but this pass no longer
    # does: those regressions are usually a JS shell served to the static client
    # (e.g. warren.k12.in.us/page/rfp came back with no title at all).
    for idx, prev in done.items():
        i = int(idx)
        if prev.get("status") == "resolved" and prev.get("rfp_url"):
            b = results.get(i)
            if (b is None or b["score"] < ACCEPT) and i not in todo:
                # results[i] may be present-but-None when every static re-fetch failed
                if not results.get(i):
                    results[i] = {"url": prev["rfp_url"], "score": 0,
                                  "signal": prev.get("signal", ""), "ev": [],
                                  "items": None, "empty_state": None, "html": ""}
                todo.append(i)
    print(f"\nstage 2: rendered re-check for {len(todo)} escalate-band agencies")
    if not todo:
        return
    logrows, lock = [], threading.Lock()

    def work(session, slice_):
        cli(session, "open", "--browser=chrome", timeout=120)
        for i, idx in enumerate(slice_, 1):
            b = results[idx]
            if not goto(session, b["url"]):
                continue
            time.sleep(SETTLE)
            d = raw_json(session, HTML_JS)
            if not d or not d.get("h"):
                continue
            s, ev, meta = C.verify_score(d["u"], d["h"])
            with lock:
                logrows.append({
                    "agency_name": agencies[idx]["agency_name"],
                    "agency_domain": agencies[idx]["agency_domain"], "url": d["u"],
                    "tier": "7r", "signal": "adjudicate-rendered", "score": s,
                    "evidence": "|".join(ev) + f"|items={meta.get('items')}|static={b['score']}",
                    "decision": "accepted" if s >= ACCEPT else "rejected",
                    "note": (meta.get("title") or "")[:120]})
                if s > b["score"]:
                    results[idx] = {"url": d["u"], "score": s, "signal": b["signal"] + "+rendered",
                                    "ev": ev, "items": meta.get("items"),
                                    "empty_state": meta.get("empty_state"), "html": d["h"]}
            if i % 10 == 0:
                with _print_lock:
                    print(f"  [{session}] {i}/{len(slice_)}", flush=True)
        cli(session, "close", timeout=60)

    slices = [todo[i::len(SESSIONS)] for i in range(len(SESSIONS))]
    ths = []
    for s, sl in zip(SESSIONS, slices):
        if sl:
            th = threading.Thread(target=work, args=(s, sl))
            th.start()
            ths.append(th)
    for th in ths:
        th.join()
    cli("w1", "close-all", timeout=60)
    C.log_candidates(logrows)


def main():
    agencies, results, done, fetched = asyncio.run(stage1())
    if "--no-render" not in sys.argv:
        stage2(agencies, results, done)

    # ---- A3 precedence, applied once, on the final winner ----
    a9inf = {}
    try:
        a9inf = json.load(open(C.OUT + "/a9_inferred.json", encoding="utf-8"))
    except FileNotFoundError:
        pass

    n_res = 0
    n_inconclusive = 0
    with open(FINAL, "w", encoding="utf-8") as f:
        for idx, a in agencies.items():
            b = results.get(idx)
            prev = done.get(str(idx)) or {}
            rec = {"idx": idx, "agency_name": a["agency_name"],
                   "cms": prev.get("cms", ""), "via": prev.get("via", ""),
                   "notes_hint": prev.get("notes_hint", ""),
                   "not_found_reason": prev.get("not_found_reason", "")}
            # An identity mismatch (R3.9) vetoes any candidate, however well it
            # scores: a high-scoring bids page for the WRONG agency is the single
            # worst output this pipeline can produce.
            if prev.get("status") == "identity-mismatch" or prev.get("identity_ok") is False:
                b = None
            if b and b["score"] >= ACCEPT:
                rec.update({"status": "resolved", "rfp_url": b["url"],
                            "score": b["score"], "signal": b["signal"],
                            "evidence": "|".join(b["ev"]), "items": b["items"],
                            "empty_state": b["empty_state"]})
                n_res += 1
            elif (prev.get("status") == "resolved" and prev.get("rfp_url")
                  and fetched.get(idx, {}).get(C.norm_url(prev["rfp_url"]), "")
                      .startswith("http")):
                # We could not re-fetch it (403/429/error) - that is not evidence
                # against the page. Keep the earlier verified capture and say so.
                why = fetched[idx][C.norm_url(prev["rfp_url"])]
                rec.update({"status": "resolved", "rfp_url": prev["rfp_url"],
                            "score": prev.get("score"), "signal": prev.get("signal", ""),
                            "evidence": prev.get("evidence", ""),
                            "items": prev.get("items"),
                            "empty_state": prev.get("empty_state"),
                            "recheck": f"re-verification inconclusive ({why} at re-check); "
                                       f"accepted on the earlier verified capture"})
                n_res += 1
                n_inconclusive += 1
            else:
                reason = prev.get("not_found_reason") or ""
                if not reason:
                    if a["lane"] == "A9" and str(idx) not in a9inf:
                        reason = ("no agency_website in input; identity could not be "
                                  "anchored and no candidate host self-identified as "
                                  "this agency in Indiana (A9)")
                    elif b:
                        reason = ("no page passed verification; best candidate "
                                  f"{b['url']} scored {b['score']} (<60)")
                    elif str(idx) in a9inf:
                        reason = "no RFP/bid listing found on the inferred site"
                    else:
                        reason = "no RFP/bid listing page found on the agency's own site"
                rec.update({"status": "unresolved", "rfp_url": "",
                            "score": b["score"] if b else None,
                            "signal": b["signal"] if b else "",
                            "evidence": "|".join(b["ev"]) if b else "",
                            "items": None, "empty_state": None,
                            "not_found_reason": reason,
                            "best_alt": [b["url"]] if b else []})
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\nADJUDICATION  resolved={n_res}/{len(agencies)}  -> {FINAL}")


if __name__ == "__main__":
    main()
