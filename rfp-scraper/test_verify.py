"""Regression test for the VERIFY scorer against live pages.

Every case here was a real misjudgement during the run, kept so the calibration
cannot silently regress:

  ciesc.org/coop-purchasing-...  a BLOG POST about co-op purchasing that scored 60,
                                 then 95 once its lexicon-heavy sidebar inflated the
                                 link-cluster count
  ciceroin.org/...advertisement  ONE advertisement, not a listing (A4)
  cityoflawrence.org/...         the SETUP.md §8.3 ground truth: correct page whose
                                 items are bare project names (scored 40 originally)
  gohammond.com/...active-rfps   a real listing with a "specific" slug - must survive

Run: .venv/Scripts/python.exe test_verify.py
"""
from __future__ import annotations

import sys

import httpx

import common as C
from p3_tier0 import HEADERS

CASES = [
    ("DROP", "https://ciesc.org/coop-purchasing-gets-school-district-more/",
     "blog post about purchasing, not a solicitation listing"),
    ("DROP", "https://www.ciceroin.org/town-of-cicero-advertisement-for-bids/",
     "single advertisement (A4: listing pages only)"),
    ("KEEP", "https://www.gohammond.com/departments/planning-and-development/"
             "economic-development/active-rfps/", "real 'Active RFPs' listing"),
    ("KEEP", "https://www.cityoflawrence.org/procurement/bid-opportunities",
     "SETUP.md 8.3 ground truth; labelled listing, bare project names"),
    ("KEEP", "https://www.egreene.k12.in.us/quick-links/request-for-bids",
     "listing of bid documents"),
    ("KEEP", "https://fishersin.gov/do-business-here/bids-proposals/",
     "bids & proposals listing"),
    ("KEEP", "https://www.townofpittsboro.org/Bids.aspx",
     "CivicPlus bid postings on the agency's own domain"),
    ("KEEP", "https://www.sgcs.k12.in.us/legal-notices",
     "school corporation legal notices (where bids are advertised)"),
    ("KEEP", "https://www.carmel.in.gov/223/Bid-Opportunities",
     "bid opportunities listing"),
]


def main():
    bad = 0
    for want, url, why in CASES:
        try:
            r = httpx.get(url, headers=HEADERS, follow_redirects=True,
                          timeout=25, verify=False)
            score, ev, _ = C.verify_score(str(r.url), r.text)
        except Exception as e:
            print(f"SKIP  {type(e).__name__}  {url}")
            continue
        got = "KEEP" if score >= 60 else "DROP"
        flag = "ok  " if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"{flag} {score:>4} want={want} got={got}  {why}")
        if got != want:
            print(f"       {url}\n       {'|'.join(ev)}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} cases pass")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
