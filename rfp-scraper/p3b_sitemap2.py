"""Phase 3b - second sitemap pass with a broadened path pattern.

Measured after the first full sweep:

  sitemap contains a lexicon hit   38 agencies -> 22 resolved (58%)
  sitemap present, no lexicon hit  52 agencies ->  0 resolved
  no sitemap at all               100 agencies ->  7 resolved (47 of them WAF-blocked)

The middle band is the cheapest remaining win: those sites *do* publish a sitemap,
but their bids page is filed under a word my lexicon did not cover
(business-office, clerk-treasurer, legal, board-of-works, document-center, ...).
This pass re-reads their sitemaps with a wider net, then VERIFYs the candidates
normally. No new signal type - just a wider bucket on the best-performing signal.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from urllib.parse import urlparse

import httpx

import common as C
from p3_tier0 import Agent, HEADERS, sitemap_urls, path_rank, ACCEPT, ESCALATE

# Wider than PATH_LEX: departments and document stores that host bid notices.
SM2 = re.compile(
    r"(business[-_/]?office|business[-_/]?service|purchas|procure|finance|fiscal"
    r"|clerk|treasurer|board[-_/]?of[-_/]?works|board[-_/]?of[-_/]?trustees"
    r"|legal|notice|document|contract|construction|capital[-_/]?project"
    r"|facilit|operation|maintenance|transportation|administration|admin"
    r"|doing[-_/]?business|opportunit|advertis|vendor|supplier|quote)", re.I)

WEIGHTS = [
    (r"(bid|rfp|rfq|solicit|procure|purchas)", 40),
    (r"(legal[-_/]?notice|public[-_/]?notice|notice)", 25),
    (r"(business[-_/]?office|doing[-_/]?business|business[-_/]?service)", 22),
    (r"(document|advertis|opportunit|vendor|supplier|quote)", 18),
    (r"(clerk|treasurer|board[-_/]?of[-_/]?works)", 14),
    (r"(finance|fiscal|contract|construction|capital)", 12),
    (r"(facilit|operation|maintenance|administration|transportation)", 6),
]
MAX_TRY = 8


def rank2(url: str) -> int:
    p = urlparse(url).path.lower()
    if C.NEG_LEX.search(p):
        return -100
    s = 0
    for pat, w in WEIGHTS:
        if re.search(pat, p):
            s += w
    if C.DETAIL_URL.search(p + "?" + (urlparse(url).query or "")):
        s -= 60
    s -= C.slug_specificity(url) * 8          # prefer listing-shaped slugs
    s += min(C.depth(url), 4) * 2
    return s


async def process(agency, sem, c1, c2):
    async with sem:
        log, cands = [], []
        site = agency["agency_website"] or ("https://" + agency["agency_domain"])
        ag = Agent(c1, c2, log)
        best = None

        try:
            locs = await sitemap_urls(ag, site)
        except Exception as e:
            locs = []
            log.append(f"ERR {type(e).__name__}")
        hits = [u for u in set(locs)
                if SM2.search(urlparse(u).path) and not C.NEG_LEX.search(urlparse(u).path)]
        hits.sort(key=rank2, reverse=True)
        hits = [u for u in hits if rank2(u) > 0][:MAX_TRY]

        for u in hits:
            r = await ag.get(u)
            if r is None or r.status_code != 200:
                continue
            if "html" not in r.headers.get("content-type", ""):
                continue
            s, ev, meta = C.verify_score(str(r.url), r.text)
            cands.append({
                "agency_name": agency["agency_name"],
                "agency_domain": agency["agency_domain"], "url": str(r.url),
                "tier": "0b", "signal": "sitemap-wide", "score": s,
                "evidence": "|".join(ev) + f"|items={meta.get('items')}",
                "decision": "accepted" if s >= ACCEPT else "rejected",
                "note": (meta.get("title") or "")[:120]})
            if best is None or (s, -C.slug_specificity(str(r.url))) > \
                    (best["score"], -C.slug_specificity(best["url"])):
                best = {"url": str(r.url), "score": s, "ev": ev, "meta": meta}
            if best["score"] >= ACCEPT and best["meta"].get("items", 0) >= 1:
                break

        C.log_candidates(cands)
        if best and best["score"] >= ACCEPT:
            C.checkpoint_write({
                "idx": agency["idx"], "agency_name": agency["agency_name"], "tier": 3,
                "status": "resolved", "rfp_url": best["url"], "score": best["score"],
                "signal": "sitemap-wide", "evidence": "|".join(best["ev"]),
                "items": best["meta"].get("items"),
                "empty_state": best["meta"].get("empty_state"), "cms": "", "via": "",
                "best_alt": [], "notes_hint": "", "home_ok": True})
            return True, len(hits)
        return False, len(hits)


async def main():
    agencies = {a["idx"]: a for a in C.load_agencies() if a["lane"] == "main"}
    done = C.checkpoint_read()
    todo = [a for i, a in agencies.items()
            if (done.get(str(i)) or {}).get("status") not in ("resolved", "identity-mismatch")]
    if "--limit" in sys.argv:
        todo = todo[:int(sys.argv[sys.argv.index("--limit") + 1])]
    print(f"sitemap-wide pass over {len(todo)} unresolved agencies")
    sem = asyncio.Semaphore(20)
    t0 = time.time()
    async with httpx.AsyncClient(verify=True) as c1, \
               httpx.AsyncClient(verify=False) as c2:
        res = await asyncio.gather(*[process(a, sem, c1, c2) for a in todo],
                                   return_exceptions=True)
    ok = sum(1 for r in res if isinstance(r, tuple) and r[0])
    tried = sum(r[1] for r in res if isinstance(r, tuple))
    errs = sum(1 for r in res if isinstance(r, Exception))
    print(f"\nSITEMAP-WIDE RESULT  newly resolved={ok}  urls tried={tried}  "
          f"errors={errs}  ({round(time.time()-t0)}s)")


if __name__ == "__main__":
    asyncio.run(main())
