"""Phase 10 helper - derive the write-up's numbers from the artifacts, not memory.

Reads out/debug_candidates.csv, out/checkpoint.jsonl and out/rfp_pages.csv and
prints the statistics WRITEUP.md quotes.
"""
from __future__ import annotations

import csv
import collections

import common as C


def main():
    cands = list(csv.DictReader(open(C.DEBUG_CSV, encoding="utf-8")))
    recs = C.checkpoint_read()
    rows = list(csv.DictReader(open(C.FINAL_CSV, encoding="utf-8")))
    ags = {a["idx"]: a for a in C.load_agencies()}

    print("=== candidates (debug_candidates.csv) ===")
    print("total scored          :", len(cands))
    print("distinct URLs         :", len({c["url"] for c in cands}))
    print("distinct agencies     :", len({c["agency_name"] for c in cands}))
    print("by tier               :", dict(collections.Counter(c["tier"] for c in cands)))
    print("by decision           :", dict(collections.Counter(c["decision"] for c in cands)))
    ok = [c for c in cands if c["decision"] == "accepted"]
    print("accepted by signal    :", dict(collections.Counter(
        c["signal"] for c in ok).most_common(12)))
    fetched = [c for c in cands if c["evidence"] and not c["evidence"].startswith("http ")]
    print("fetched OK / non-200  :", len(fetched), "/", len(cands) - len(fetched))
    http_err = collections.Counter(c["evidence"] for c in cands
                                   if c["evidence"].startswith("http "))
    print("non-200 breakdown     :", dict(http_err.most_common(6)))

    print("\n=== outcome (rfp_pages.csv) ===")
    filled = [r for r in rows if r["rfp_url"]]
    print("rows                  :", len(rows))
    print("with rfp_url          :", len(filled), f"({round(100*len(filled)/len(rows))}%)")
    print("hosting               :", dict(collections.Counter(r["rfp_hosting"] for r in rows)))
    print("by agency_type        :", {
        t: f"{sum(1 for r in rows if r['agency_type']==t and r['rfp_url'])}/"
           f"{sum(1 for r in rows if r['agency_type']==t)}"
        for t in sorted({r["agency_type"] for r in rows})})
    plats = collections.Counter(r["rfp_platform"] for r in filled
                                if r["rfp_platform"] != "self-hosted")
    print("third-party platforms :", dict(plats.most_common(12)))

    print("\n=== which tier resolved each agency ===")
    tier_of = {}
    for k, r in recs.items():
        if r.get("status") == "resolved":
            tier_of[int(k)] = r.get("tier")
    print("resolved by tier      :", dict(collections.Counter(tier_of.values())))
    print("signals of winners    :", dict(collections.Counter(
        r["signal"] for r in recs.values() if r.get("status") == "resolved").most_common(14)))

    print("\n=== unresolved diagnosis ===")
    un = [int(k) for k, r in recs.items() if r.get("status") != "resolved"]
    un += [a["idx"] for a in ags.values() if str(a["idx"]) not in recs]
    print("unresolved agencies   :", len(set(un)))
    blocked = [r for r in recs.values() if r.get("status") != "resolved"
               and "blocked" in (r.get("notes_hint") or "")]
    nohome = [r for r in recs.values() if r.get("status") != "resolved"
              and not r.get("home_ok")]
    print("  of which blocked    :", len(blocked))
    print("  homepage unreachable:", len(nohome))
    scored = [r for r in recs.values() if r.get("status") != "resolved"
              and r.get("score") is not None]
    print("  had some candidate  :", len(scored))
    print("  best-candidate score:", dict(collections.Counter(
        (r["score"] // 10) * 10 for r in scored).most_common()))
    a9 = [a for a in ags.values() if a["lane"] == "A9"]
    print("A9 lane rows          :", len(a9),
          "resolved:", sum(1 for a in a9
                           if (recs.get(str(a["idx"])) or {}).get("status") == "resolved"))

    print("\n=== effort ===")
    f0 = [r.get("fetches") for r in recs.values() if r.get("fetches")]
    g1 = [r.get("gotos") for r in recs.values() if r.get("gotos")]
    if f0:
        print(f"tier0 fetches/agency  : mean {sum(f0)/len(f0):.1f} max {max(f0)}")
    if g1:
        print(f"tier1 page loads/agency: mean {sum(g1)/len(g1):.1f} max {max(g1)}")


if __name__ == "__main__":
    main()
