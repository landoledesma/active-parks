"""Phase 3c - second path-probe pass with an evidence-derived path list.

Derived from what actually won, not from guesswork. Of the first 27 resolutions,
11 were a "public notices" / "legal notices" page sitting under a parent that
varies per site, and often with underscores rather than hyphens:

  /school-board/public-notices-information      /school_board/public_notices
  /about/school-board/public-notices            /central-office/public-notices
  /community/public-notices                     /district_info/bids_requests_for_proposal
  /public-notices-home                          /legal-notices

Indiana school corporations advertise bids as statutory legal notices (IC 5-3-1),
so this is the dominant shape for the 100 districts in the input. The original
canonical list only had the bare `/legal-notices` and `/public-notices`.

Static, polite, and cheap: parent x leaf combinations, HEAD-free, VERIFY as usual.
"""
from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import urljoin

import httpx

import common as C
from p3_tier0 import Agent, ACCEPT, ESCALATE

PARENTS = ["", "/about", "/about-us", "/district", "/our-district", "/district-info",
           "/district_info", "/school-board", "/school_board", "/board",
           "/central-office", "/community", "/departments", "/department",
           "/business", "/business-office", "/administration", "/government",
           "/services", "/quick-links", "/resources"]

LEAVES = ["/public-notices", "/public_notices", "/legal-notices", "/legal_notices",
          "/public-notice", "/legal-notice", "/notices",
          "/bids", "/bids-rfps", "/bids_rfps", "/bid-opportunities",
          "/request-for-bids", "/requests-for-proposals", "/rfps",
          "/purchasing", "/procurement",
          "/bids-requests-for-proposal", "/bids_requests_for_proposal",
          "/public-notices-home"]

# Full cross product is too many requests; use the high-yield subset.
def build_paths():
    out, seen = [], set()
    # bare leaves first (cheapest, most likely)
    for lf in LEAVES:
        if lf not in seen:
            seen.add(lf)
            out.append(lf)
    for p in PARENTS[1:]:
        for lf in ["/public-notices", "/public_notices", "/legal-notices",
                   "/bids", "/purchasing", "/bid-opportunities", "/rfps"]:
            u = p + lf
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out


PATHS = build_paths()
BUDGET = 70


async def process(agency, sem, c1, c2):
    async with sem:
        log, cands = [], []
        site = agency["agency_website"] or ("https://" + agency["agency_domain"])
        ag = Agent(c1, c2, log)
        best = None
        for p in PATHS:
            if ag.used >= BUDGET:
                break
            url = urljoin(site, p)
            r = await ag.get(url)
            if r is None or r.status_code != 200:
                continue
            if "html" not in r.headers.get("content-type", ""):
                continue
            s, ev, meta = C.verify_score(str(r.url), r.text)
            cands.append({
                "agency_name": agency["agency_name"],
                "agency_domain": agency["agency_domain"], "url": str(r.url),
                "tier": "0c", "signal": "path-probe2", "score": s,
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
                "signal": "path-probe2", "evidence": "|".join(best["ev"]),
                "items": best["meta"].get("items"),
                "empty_state": best["meta"].get("empty_state"), "cms": "", "via": "",
                "best_alt": [], "notes_hint": "", "home_ok": True})
            return True
        return False


async def main():
    agencies = {a["idx"]: a for a in C.load_agencies() if a["lane"] == "main"}
    done = C.checkpoint_read()
    todo = [a for i, a in agencies.items()
            if (done.get(str(i)) or {}).get("status") not in ("resolved", "identity-mismatch")]
    if "--limit" in sys.argv:
        todo = todo[:int(sys.argv[sys.argv.index("--limit") + 1])]
    print(f"path-probe2: {len(todo)} agencies x up to {len(PATHS)} paths")
    sem = asyncio.Semaphore(12)
    t0 = time.time()
    async with httpx.AsyncClient(verify=True) as c1, \
               httpx.AsyncClient(verify=False) as c2:
        res = await asyncio.gather(*[process(a, sem, c1, c2) for a in todo],
                                   return_exceptions=True)
    ok = sum(1 for r in res if r is True)
    errs = [r for r in res if isinstance(r, Exception)]
    print(f"\nPATH-PROBE2 RESULT  newly resolved={ok}  errors={len(errs)}  "
          f"({round(time.time()-t0)}s)")
    if errs:
        print("  sample error:", type(errs[0]).__name__, errs[0])


if __name__ == "__main__":
    asyncio.run(main())
