"""Phase 8 - the A9 lane: 10 agencies with no agency_website and no agency_domain.

A5 forbids search engines, and with no domain there is no identity anchor, so the
default outcome is `not-found`. PLAN.md A9 allows a flagged stretch attempt, which
this implements *deterministically*:

  1. generate candidate hostnames from the agency name (no external lookup),
  2. fetch each candidate,
  3. accept ONLY on strong self-identification: the page must name the agency
     (a distinctive token from its name) AND place it in Indiana,
  4. only then run the normal discovery ladder on the confirmed host.

Every row resolved this way is flagged "domain inferred - VERIFY MANUALLY".
`--probe` reports without writing checkpoints; `--commit` writes them.
"""
from __future__ import annotations

import asyncio
import re
import sys

import httpx

import common as C
import p3_tier0 as T0

STOP = {"school", "schools", "corp", "corporation", "community", "county",
        "consolidated", "metropolitan", "district", "the", "of", "public",
        "academy", "center", "centre", "career", "area", "city", "town"}


def tokens(name: str):
    return [w for w in re.findall(r"[a-z0-9]+", name.lower())]


def distinctive(name: str):
    """Tokens that actually identify the agency (drops generic school words)."""
    t = [w for w in tokens(name) if w not in STOP and len(w) > 2]
    return t or tokens(name)


def candidates(agency: dict):
    """Full-name slug FIRST: "Oakland City" and "Clay City" are city names that
    contain the word "city", so the distinctive-token slug alone (oakland/clay)
    probed the wrong hosts entirely."""
    name = agency["agency_name"]
    t = [w for w in tokens(name) if w not in {"of", "the"}]
    d = distinctive(name)
    full = "".join(t)                 # oaklandcity, southbendcommunityschoolcorp
    fulld = "-".join(t)
    slug = "".join(d)                 # oakland,     southbend
    dash = "-".join(d)
    acro = "".join(w[0] for w in t)   # gccs, sbcsc, tlja
    # district acronyms are often built from the distinctive words only
    acrod = "".join(w[0] for w in d)
    out = []
    if agency["agency_type"] == "city":
        for h in (f"{full}.in.gov", f"www.{full}.in.gov", f"{fulld}.in.gov",
                  f"cityof{full}.com", f"cityof{full}.org", f"townof{full}.org",
                  f"{full}.org", f"{full}.com", f"{full}indiana.com",
                  f"{full}-in.gov", f"{full}.in.us",
                  f"{slug}.in.gov", f"cityof{slug}.com", f"townof{slug}.org",
                  f"{slug}.org", f"{slug}.com", f"{dash}.in.gov"):
            out.append(h)
    else:
        # Indiana school corporations cluster on .k12.in.us and on acronym domains.
        for h in (f"{acro}.k12.in.us", f"{acrod}.k12.in.us", f"{full}.k12.in.us",
                  f"{slug}.k12.in.us", f"{acro}schools.org", f"{acrod}schools.org",
                  f"{full}.org", f"{full}schools.org", f"{slug}schools.org",
                  f"{acro}.org", f"{acrod}.org", f"{full}.com",
                  f"{fulld}.org", f"{slug}.org", f"{acro}.us", f"{full}.net",
                  f"{acro}schools.k12.in.us", f"{acrod}schools.k12.in.us",
                  f"{slug}sc.k12.in.us", f"{slug}csc.k12.in.us",
                  f"{slug}cs.k12.in.us", f"{acro}sc.k12.in.us",
                  f"{acrod}sc.k12.in.us", f"{acrod}csc.k12.in.us",
                  f"{slug}.net", f"{slug}.us", f"{acrod}.net", f"{acro}.net",
                  f"{slug}schools.com", f"{acrod}schools.com",
                  f"{slug}.in.us", f"{acrod}.us"):
            out.append(h)
    seen, ded = set(), []
    for h in out:
        if h not in seen:
            seen.add(h)
            ded.append(h)
    return ded


def required_tokens(name: str):
    """EVERY meaningful word of the name must appear. Dropping generic words is
    what let `southeastern.org` pass for "Southeastern Career Center" and
    `southbend.net` (the *city*) pass for "South Bend Community School Corp"."""
    out = []
    for w in tokens(name):
        if w in {"of", "the", "a"}:
            continue
        out.append("corporat" if w in {"corp", "corporation"} else w)
    return out


def identifies(html: str, agency: dict):
    """Strong self-identification, per A9. Returns (ok, evidence).

    Deliberately strict: a wrong-entity match is the exact invisible error this
    work sample is about, and an unresolved row costs less than a fabricated one.
    """
    text = C.strip_html(html).lower()
    head = C.title_h1(html).lower()
    if len(text) < 200:
        return False, "page too small"

    req = required_tokens(agency["agency_name"])
    missing = [w for w in req if w not in text]
    in_indiana = bool(re.search(r"\bindiana\b", text) or
                      re.search(r",\s*in\s+4\d{4}", text) or
                      re.search(r"\bin\s+4\d{4}\b", text))
    # the page must also be the right *kind* of entity
    if agency["agency_type"] == "city":
        kind = bool(re.search(r"\b(city|town)\s+of\b|\btown\s+council\b|\bmayor\b", text))
    else:
        kind = bool(re.search(r"\bschool\s+(corporation|district|corp)\b|\bschools?\b"
                              r"|\bsuperintendent\b", text))
    head_named = any(w in head for w in req if len(w) > 3)
    ok = not missing and in_indiana and kind and head_named
    ev = (f"missing={missing or 'none'}; indiana={in_indiana}; "
          f"kind={kind}; in_title={head_named}")
    return ok, ev


def dns_ok(host: str) -> bool:
    """Cheap pre-filter: most generated hostnames simply do not exist, and a DNS
    miss costs microseconds against a 15 s connect timeout."""
    import socket
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except Exception:
        return False


async def resolve(agency, client, client2):
    log = []
    hosts = [h for h in candidates(agency) if dns_ok(h)]
    log.append(f"{len(hosts)} of {len(candidates(agency))} candidate hosts resolve in DNS: "
               + ", ".join(hosts[:12]))
    for host in hosts:
        url = "https://" + host
        r = None
        for cl in (client, client2):
            try:
                r = await cl.get(url, headers={"User-Agent": C.UA},
                                 follow_redirects=True, timeout=10.0)
                break
            except Exception:
                continue
        if r is None or r.status_code != 200:
            log.append(f"{url} -> {r.status_code if r is not None else 'ERR'}")
            continue
        if "html" not in r.headers.get("content-type", ""):
            continue
        ok, ev = identifies(r.text, agency)
        log.append(f"{url} -> 200 {'MATCH' if ok else 'no'} ({ev})")
        if ok:
            return host, str(r.url), r.text, log
    return None, None, None, log


async def main():
    commit = "--commit" in sys.argv
    a9 = [a for a in C.load_agencies() if a["lane"] == "A9"]
    print(f"A9 lane: {len(a9)} agencies with no website/domain in the input\n")
    limits = httpx.Limits(max_connections=20)
    confirmed = []
    async with httpx.AsyncClient(limits=limits, verify=True) as c1, \
               httpx.AsyncClient(limits=limits, verify=False) as c2:
        for a in a9:
            host, final, html, log = await resolve(a, c1, c2)
            print(f"[{a['idx']:3}] {a['agency_name']}  ({a['agency_type']})")
            for line in log[-4:]:
                print("      ", line[:150])
            if host:
                print(f"       => CONFIRMED {final}")
                confirmed.append((a, host, final))
            else:
                print("       => no self-identifying host among "
                      f"{len(candidates(a))} candidates")
            print()

    print(f"confirmed {len(confirmed)}/{len(a9)}")
    if not commit:
        print("(dry run - pass --commit to run discovery on confirmed hosts)")
        return

    # Persist the inferred identity so phase 9 can classify hosting against it
    # (the input has no agency_domain for these rows) and flag every such row.
    import json as _json
    with open(C.OUT + "/a9_inferred.json", "w", encoding="utf-8") as f:
        _json.dump({str(a["idx"]): {"agency_name": a["agency_name"],
                                    "inferred_website": final,
                                    "inferred_domain": host.replace("www.", "")}
                    for a, host, final in confirmed}, f, indent=1)

    # Run the normal Tier-0 ladder against each confirmed host.
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(verify=True) as c1, \
               httpx.AsyncClient(verify=False) as c2:
        tasks = []
        for a, host, final in confirmed:
            a2 = dict(a)
            a2["agency_website"] = final
            a2["agency_domain"] = host.replace("www.", "")
            tasks.append(T0.process(a2, sem, c1, c2))
        recs = await asyncio.gather(*tasks, return_exceptions=True)

    for (a, host, final), rec in zip(confirmed, recs):
        if isinstance(rec, Exception):
            print(f"  {a['agency_name']}: {type(rec).__name__}")
            continue
        print(f"  {a['agency_name']}: {rec['status']} {rec['score']} {rec['rfp_url']}")

    # Every A9 agency gets an explicit checkpoint line with the A9 provenance.
    resolved_idx = {a["idx"] for a, _, _ in confirmed}
    for a in a9:
        if a["idx"] in resolved_idx:
            continue
        C.checkpoint_write({
            "idx": a["idx"], "agency_name": a["agency_name"], "tier": 8,
            "status": "unresolved", "rfp_url": "", "score": None, "signal": "",
            "evidence": "", "items": None, "empty_state": None, "cms": "", "via": "",
            "best_alt": [], "home_ok": False, "notes_hint": "",
            "not_found_reason": ("no agency_website in input; identity could not be "
                                 "anchored and no candidate host self-identified "
                                 "as this agency in Indiana (A9)")})


if __name__ == "__main__":
    asyncio.run(main())
