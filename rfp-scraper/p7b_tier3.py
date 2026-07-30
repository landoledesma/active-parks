"""Phase 7b - Tier 3, the agentic tail.

PLAN.md routes the 30-59 VERIFY band to human/agent judgement. 17 agencies landed
there after adjudication. Each page was opened and read; the decision and the
evidence that drove it are recorded here as data, not buried in a transcript.

The recurring pattern is worth stating: an Indiana "Public/Legal Notices" page is
a *statutory notices* page. Sometimes it carries bid advertisements (accept), and
sometimes it carries only budget and tax notices (reject). The URL and title are
identical in both cases, which is exactly why this band cannot be settled by
scoring alone.

Writes out/tier3_decisions.json, which phase 9 applies last.
"""
from __future__ import annotations

import json

import common as C

ACCEPT = {
    110: ("https://www.wayne.k12.in.us/community/public-notices",
          "page lists 'Request for Proposal Projects' and 'View Request for "
          "Proposals' - solicitations are published here"),
    67: ("https://www.uc.k12.in.us/public-notices",
         "page lists a live solicitation: 'Request For Proposal - Food Operation "
         "and Management Services'"),
}

REJECT = {
    57:  "blog post about cooperative purchasing, not a solicitation listing",
    10:  "notices page carries only tax/budget notices (Notice to Taxpayers, "
         "Additional Appropriation) - no solicitations published here",
    166: "notices page carries only budget notices (2025/2026 Budget Notice to "
         "Tax Payers) - no solicitations published here",
    107: "notices page carries only public-hearing and taxpayer notices - no "
         "solicitations published here",
    163: "single bid advertisement for one project, not a listing page (A4)",
    1:   "single 'Notice of Public Bid' from Sept 2021, not a listing page (A4)",
    151: "single Request for Proposals (banking, Mar 2024) posted as a news item, "
         "not a listing page (A4)",
    48:  "public-notices page reachable but contains no solicitation entries",
    190: "public-notices page reachable but contains no solicitation entries",
    114: "public-notices page reachable but contains no solicitation entries",
    171: "public-notice page reachable but contains no solicitation entries",
    58:  "finance department page, no solicitation entries",
    135: "business page, no solicitation entries",
    93:  "legal-notices page reachable but contains no solicitation entries",
    38:  "site-search result page quoting the public-records bid policy, not a "
         "solicitation listing",
}


def main():
    ags = {a["idx"]: a for a in C.load_agencies()}
    out, rows = {}, []
    for idx, (url, why) in ACCEPT.items():
        out[str(idx)] = {"status": "resolved", "rfp_url": url, "signal": "tier3-agent",
                         "tier3_note": why, "score": 60}
        rows.append({"agency_name": ags[idx]["agency_name"],
                     "agency_domain": ags[idx]["agency_domain"], "url": url,
                     "tier": "3", "signal": "tier3-agent", "score": 60,
                     "evidence": why, "decision": "accepted", "note": "agent read the page"})
    for idx, why in REJECT.items():
        out[str(idx)] = {"status": "unresolved", "rfp_url": "",
                         "not_found_reason": why, "signal": "tier3-agent"}
        rows.append({"agency_name": ags[idx]["agency_name"],
                     "agency_domain": ags[idx]["agency_domain"],
                     "url": "", "tier": "3", "signal": "tier3-agent", "score": 0,
                     "evidence": why, "decision": "rejected", "note": "agent read the page"})
    C.log_candidates(rows)
    with open(C.OUT + "/tier3_decisions.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"tier3: {len(ACCEPT)} accepted, {len(REJECT)} rejected -> "
          f"out/tier3_decisions.json")
    for idx, (url, why) in ACCEPT.items():
        print(f"  ACCEPT [{idx}] {ags[idx]['agency_name']}: {url}")


if __name__ == "__main__":
    main()
