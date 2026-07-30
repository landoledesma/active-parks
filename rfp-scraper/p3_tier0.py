"""Phase 3+4 - Tier 0 static discovery with inline VERIFY.

Signal ladder (PLAN.md, measured order):
  1 sitemap.xml / sitemap index (via robots.txt too)
  2 known-portal outbound links from the homepage (fingerprint dictionary)
  3 canonical path probing
  4 CMS-specific templates
  5 site-search endpoint
  6 homepage link extraction, then depth-2   <- corroboration only, 0/10 measured

Every fetched candidate is scored by common.verify_score (VERIFY is mandatory) and
written to out/debug_candidates.csv. One checkpoint line per agency.
Politeness: 20 agencies in parallel, strictly 1 request at a time per domain,
0.3 s spacing, 15 s timeout, 2 retries, realistic UA, hard per-agency fetch budget.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import httpx

import common as C

GLOBAL_CONC = 20
BUDGET = 55            # max fetches per agency (measured need; see PLAN note)
TIMEOUT = 15.0
RETRIES = 2
SPACING = 0.3          # seconds between requests to the same domain
ACCEPT = 60
ESCALATE = 30
MAX_SITEMAP_BYTES = 6_000_000

# Complete, realistic Chrome header set. A6: this is WAF *compatibility* (many
# WAFs 403 a request missing Sec-Fetch-*), not evasion — we still rate-limit,
# honour the block after bounded retries, and document it.
HEADERS = {
    "User-Agent": C.UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}

HUB_HINTS = re.compile(
    r"(doing[-_/ ]?business|business|government|departments?|finance|administration"
    r"|city[-_/ ]?hall|services|district|about)", re.I)


class Agent:
    """Per-agency fetcher: sequential, budgeted, retrying, TLS-tolerant."""

    def __init__(self, client, client_nossl, log):
        self.c, self.c2, self.log = client, client_nossl, log
        self.used = 0
        self.tls_fallback = False
        self.blocked = []
        self.seen = set()

    async def get(self, url, allow_big=False):
        if self.used >= BUDGET:
            return None
        n = C.norm_url(url)
        if n in self.seen:
            return None
        self.seen.add(n)
        self.used += 1
        client = self.c2 if self.tls_fallback else self.c
        for attempt in range(RETRIES + 1):
            try:
                r = await client.get(url, headers=HEADERS, follow_redirects=True,
                                     timeout=TIMEOUT)
                await asyncio.sleep(SPACING)
                if r.status_code in (403, 406, 429, 503):
                    self.blocked.append(f"{r.status_code} {url}")
                    return r
                return r
            except (httpx.ConnectError, httpx.ReadError) as e:
                if "SSL" in type(e).__name__ or "certificate" in str(e).lower() \
                        or "SSL" in str(e):
                    if not self.tls_fallback:
                        self.tls_fallback = True
                        client = self.c2
                        continue
                if attempt == RETRIES:
                    self.log.append(f"ERR {type(e).__name__} {url}")
                    return None
                await asyncio.sleep(0.8 * (attempt + 1))
            except Exception as e:
                if attempt == RETRIES:
                    self.log.append(f"ERR {type(e).__name__} {url}")
                    return None
                await asyncio.sleep(0.8 * (attempt + 1))
        return None


def detect_cms(html: str) -> str:
    m = re.search(r'(?is)<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', html or "")
    gen = (m.group(1) if m else "").strip()
    blob = ((html or "")[:200000] + " " + gen).lower()
    for key, label in [
        ("civicengage", "CivicPlus"), ("civicplus", "CivicPlus"), ("wordpress", "WordPress"),
        ("edlio", "Edlio"), ("finalsite", "Finalsite"), ("apptegy", "Apptegy"),
        ("drupal", "Drupal"), ("squarespace", "Squarespace"), ("revize", "Revize"),
        ("granicus", "Granicus"), ("municode", "Municode"), ("schoolwires", "Schoolwires"),
        ("blackboard", "Blackboard"), ("campussuite", "CampusSuite"), ("wix.com", "Wix"),
        ("weebly", "Weebly"), ("joomla", "Joomla"), ("sharepoint", "SharePoint"),
    ]:
        if key in blob:
            return label + (f" ({gen})" if gen and key in gen.lower() else "")
    return gen


def links_of(html: str, base: str):
    out = []
    for m in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>', html or ""):
        href, inner = m.group(1), C.strip_html(m.group(2))
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            out.append((urljoin(base, href), inner.strip()))
        except Exception:
            continue
    return out


def path_rank(url: str) -> int:
    """Cheap pre-score used to order candidates before spending a fetch."""
    p = urlparse(url).path.lower()
    if C.NEG_LEX.search(p):
        return -100
    s = 0
    if C.DETAIL_URL.search(p + "?" + urlparse(url).query):
        s -= 60                            # A4: detail pages are not the answer
    if re.search(r"(bid|rfp|rfq|solicit|procure|purchas)", p):
        s += 30
    if re.search(r"(opportunit|posting|current|open)", p):
        s += 10
    if re.search(r"(doing[-_]?business|legal[-_]?notice|public[-_]?notice|vendor)", p):
        s += 12
    if p.endswith(".pdf"):
        s -= 25
    s += min(C.depth(url), 4) * 3          # prefer deeper, more specific pages
    return s


async def sitemap_urls(ag: Agent, base: str):
    """robots.txt Sitemap: entries + /sitemap.xml family; follow index, cap 3 children."""
    found, roots = [], []
    r = await ag.get(urljoin(base, "/robots.txt"))
    if r is not None and r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
        for m in re.finditer(r"(?im)^\s*sitemap:\s*(\S+)", r.text[:100000]):
            roots.append(m.group(1).strip())
    for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap-index.xml"):
        u = urljoin(base, p)
        if u not in roots:
            roots.append(u)

    checked = 0
    queue = list(roots)
    while queue and checked < 5:
        u = queue.pop(0)
        checked += 1
        r = await ag.get(u)
        if r is None or r.status_code != 200:
            continue
        body = r.content[:MAX_SITEMAP_BYTES]
        if u.endswith(".gz") or body[:2] == b"\x1f\x8b":
            try:
                body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(MAX_SITEMAP_BYTES)
            except Exception:
                continue
        try:
            txt = body.decode("utf-8", "ignore")
        except Exception:
            continue
        if "<sitemapindex" in txt[:4000].lower():
            children = re.findall(r"(?is)<loc>\s*([^<\s]+)\s*</loc>", txt)
            # prefer child sitemaps whose name suggests pages/content
            children.sort(key=lambda c: (0 if re.search(r"(page|post|content|main|sitemap1)", c, re.I) else 1))
            queue = children[:3] + queue
            continue
        locs = re.findall(r"(?is)<loc>\s*([^<\s]+)\s*</loc>", txt)
        found.extend(locs)
        if len(found) > 60000:
            break
    return found


async def process(agency: dict, sem, client, client_nossl):
    async with sem:
        t0 = time.time()
        log, cands, taxo = [], [], []
        site = agency["agency_website"] or ("https://" + agency["agency_domain"])
        ag = Agent(client, client_nossl, log)
        best = None
        cms = ""

        def record(url, signal, score, ev, meta, link_text=""):
            nonlocal best
            dec = "accepted" if score >= ACCEPT else (
                "escalate" if score >= ESCALATE else "rejected")
            cands.append({
                "agency_name": agency["agency_name"],
                "agency_domain": agency["agency_domain"],
                "url": url, "tier": "0", "signal": signal, "score": score,
                "evidence": "|".join(ev) + f"|items={meta.get('items')}",
                "decision": dec,
                "note": (meta.get("title") or "")[:120],
            })
            cand = {"url": url, "signal": signal, "score": score, "ev": ev,
                    "meta": meta, "link_text": link_text}
            if best is None or (score, C.depth(url)) > (best["score"], C.depth(best["url"])):
                best = cand
            return score

        async def try_url(url, signal, link_text=""):
            r = await ag.get(url)
            if r is None:
                return -1
            final = str(r.url)
            if r.status_code != 200:
                cands.append({
                    "agency_name": agency["agency_name"],
                    "agency_domain": agency["agency_domain"], "url": url, "tier": "0",
                    "signal": signal, "score": -1, "evidence": f"http {r.status_code}",
                    "decision": "rejected", "note": ""})
                return -1
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype:
                return -1
            html = r.text
            s, ev, meta = C.verify_score(final, html, link_text)
            meta["html"] = html
            # A4 "normalize upward": a bid-related detail page usually sits in a
            # category/tag archive, which IS the listing page we want.
            if meta.get("detail") and (C.LEX_STRONG.search(meta.get("title", ""))
                                       or C.LEX_STRONG.search(link_text or "")):
                for u2, t2 in links_of(html, final):
                    if re.search(r"/(category|tag|topic|taxonomy/term|archives?)/", u2, re.I) \
                            and not C.NEG_LEX.search(u2 + " " + t2):
                        taxo.append((u2, t2))
            return record(final, signal, s, ev, meta, link_text)

        # ---------- homepage ----------
        home_html = ""
        home_final = site
        r = await ag.get(site)
        if r is None or r.status_code != 200:
            # try scheme/www variants before declaring dead (playbook)
            h = C.host_of(site)
            alts = []
            if h.startswith("www."):
                alts.append("https://" + h[4:])
            else:
                alts.append("https://www." + h)
            alts.append("http://" + h)
            for a in alts:
                r2 = await ag.get(a)
                if r2 is not None and r2.status_code == 200:
                    r, site = r2, a
                    break
        ident_ok, ident_ev = True, ""
        if r is not None and r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            home_html, home_final = r.text, str(r.url)
            cms = detect_cms(home_html)
            # Identity guard: if the input's URL redirected somewhere that places
            # itself in another state, this is not our agency (see indiana_identity).
            if C.registrable(C.host_of(home_final)) != C.registrable(C.host_of(site)):
                ident_ok, ident_ev = C.indiana_identity(home_html)
                if not ident_ok:
                    ident_ev = (f"input website redirects to "
                                f"{C.host_of(home_final)} which {ident_ev}")
        base = home_final if home_html else site

        home_links = links_of(home_html, base) if home_html else []

        # ---------- 1. sitemap ----------
        try:
            locs = await sitemap_urls(ag, base)
        except Exception as e:
            locs, _ = [], log.append(f"ERR sitemap {type(e).__name__}")
        sm_hits = []
        for u in locs:
            if C.PATH_LEX.search(urlparse(u).path) and not C.NEG_LEX.search(urlparse(u).path):
                sm_hits.append(u)
        sm_hits = sorted(set(sm_hits), key=path_rank, reverse=True)[:6]
        log.append(f"sitemap locs={len(locs)} hits={len(sm_hits)}")
        for u in sm_hits:
            await try_url(u, "sitemap")
            if best and best["score"] >= ACCEPT and best["meta"].get("items", 0) >= 1:
                break

        # ---------- 2. known-portal outbound links (fingerprint dictionary) ----------
        if not best or best["score"] < ACCEPT:
            for u, txt in home_links:
                if C.portal_of(u) and not C.same_org(u, agency["agency_domain"], site):
                    if C.portal_of(u) == "BoardDocs":
                        continue          # board agendas are not a bid listing
                    await try_url(u, "portal-link", txt)
                    if best and best["score"] >= ACCEPT:
                        break

        # ---------- 3. canonical path probing ----------
        if not best or best["score"] < ACCEPT:
            for p in C.CANONICAL_PATHS:
                if ag.used >= BUDGET - 6:
                    break
                await try_url(urljoin(base, p), "path-probe")
                if best and best["score"] >= ACCEPT and best["meta"].get("items", 0) >= 1:
                    break

        # ---------- 4. CMS templates ----------
        if (not best or best["score"] < ACCEPT) and cms:
            key = cms.lower().split()[0]
            for k, paths in C.CMS_TEMPLATES.items():
                if k in cms.lower():
                    for p in paths:
                        await try_url(urljoin(base, p), f"cms:{k}")
                        if best and best["score"] >= ACCEPT:
                            break
                    break

        # ---------- 4b. eGov Strategies document centre (bids = a document type) ----------
        if (not best or best["score"] < ACCEPT) and "/egov/" in (home_html or "")[:300000].lower():
            center = urljoin(base, C.EGOV_CENTER)
            r = await ag.get(center)
            if r is not None and r.status_code == 200:
                sel = re.search(r'(?is)<select[^>]*name=["\']?eGov_searchType["\']?[^>]*>(.*?)</select>',
                                r.text)
                if sel:
                    for v, t in re.findall(
                            r'(?is)<option[^>]*value=["\']?([^"\'>\s]*)["\']?[^>]*>(.*?)</option>',
                            sel.group(1)):
                        tt = C.strip_html(t).strip()
                        if v and re.search(r"(bid|rfp|rfq|purchas|procure|solicit|proposal)",
                                           tt, re.I):
                            await try_url(center + C.EGOV_QUERY.format(v),
                                          "cms:egov-doccenter", tt)
                            break
                    else:
                        log.append("egov: no bid document type")

        # ---------- 4c. taxonomy archives harvested from detail hits (A4) ----------
        if (not best or best["score"] < ACCEPT) and taxo:
            uniq, seen_t = [], set()
            for u, t in taxo:
                n = C.norm_url(u)
                if n not in seen_t:
                    seen_t.add(n)
                    uniq.append((u, t))
            uniq.sort(key=lambda x: path_rank(x[0]) + (20 if C.LEX_STRONG.search(x[1]) else 0),
                      reverse=True)
            for u, t in uniq[:3]:
                await try_url(u, "taxonomy-archive", t)
                if best and best["score"] >= ACCEPT:
                    break

        # ---------- 5. site search ----------
        if not best or best["score"] < ACCEPT:
            for p in C.SEARCH_TEMPLATES[:3]:
                r = await ag.get(urljoin(base, p))
                if r is None or r.status_code != 200:
                    continue
                hits = [(u, t) for u, t in links_of(r.text, str(r.url))
                        if C.PATH_LEX.search(urlparse(u).path)
                        and not C.NEG_LEX.search(urlparse(u).path)
                        and C.same_org(u, agency["agency_domain"], site)]
                hits.sort(key=lambda x: path_rank(x[0]), reverse=True)
                for u, t in hits[:3]:
                    await try_url(u, "site-search", t)
                if best and best["score"] >= ACCEPT:
                    break

        # ---------- 6. homepage links, then depth-2 (corroboration only) ----------
        if not best or best["score"] < ACCEPT:
            cand_links = [(u, t) for u, t in home_links
                          if (C.PATH_LEX.search(urlparse(u).path) or C.LEX_STRONG.search(t))
                          and not C.NEG_LEX.search(urlparse(u).path + " " + t)]
            cand_links.sort(key=lambda x: path_rank(x[0]) + (20 if C.LEX_STRONG.search(x[1]) else 0),
                            reverse=True)
            for u, t in cand_links[:4]:
                await try_url(u, "home-link", t)
                if best and best["score"] >= ACCEPT:
                    break

        if (not best or best["score"] < ACCEPT) and home_links and ag.used < BUDGET - 4:
            hubs = [(u, t) for u, t in home_links
                    if C.same_org(u, agency["agency_domain"], site)
                    and HUB_HINTS.search(t) and not C.NEG_LEX.search(u + " " + t)]
            seen_h = set()
            for u, t in hubs[:3]:
                if C.norm_url(u) in seen_h:
                    continue
                seen_h.add(C.norm_url(u))
                r = await ag.get(u)
                if r is None or r.status_code != 200:
                    continue
                sub = [(x, y) for x, y in links_of(r.text, str(r.url))
                       if (C.PATH_LEX.search(urlparse(x).path) or C.LEX_STRONG.search(y))
                       and not C.NEG_LEX.search(urlparse(x).path + " " + y)]
                sub.sort(key=lambda x: path_rank(x[0]) + (20 if C.LEX_STRONG.search(x[1]) else 0),
                         reverse=True)
                for x, y in sub[:2]:
                    await try_url(x, "depth2-link", y)
                if best and best["score"] >= ACCEPT:
                    break

        # ---------- A3: self-hosted page that only links out to a portal ----------
        via = ""
        if best and best["score"] >= ACCEPT and best["meta"].get("items", 0) == 0 \
                and C.same_org(best["url"], agency["agency_domain"], site):
            for u, txt in links_of(best["meta"].get("html", ""), best["url"]):
                vend = C.portal_of(u)
                if vend and vend != "BoardDocs" and not C.same_org(u, agency["agency_domain"], site):
                    prev = best
                    s = await try_url(u, "portal-via-page", txt)
                    if s >= ACCEPT:
                        via = prev["url"]
                    break

        # ---------- result ----------
        status, url, note_bits = "unresolved", "", []
        if best and best["score"] >= ACCEPT:
            status, url = "resolved", best["url"]
        elif best and best["score"] >= ESCALATE:
            status = "escalate"
        if not ident_ok:
            # Refuse to emit a URL for an agency we cannot confirm is the right one.
            status, url = "identity-mismatch", ""
            note_bits.append(ident_ev)
        if ag.blocked:
            note_bits.append("blocked:" + ag.blocked[0])
        if ag.tls_fallback:
            note_bits.append("tls-unverified")
        if not home_html:
            note_bits.append("homepage-unreachable")

        rec = {
            "idx": agency["idx"], "agency_name": agency["agency_name"],
            "tier": 0, "status": status, "rfp_url": url,
            "score": best["score"] if best else None,
            "signal": best["signal"] if best else "",
            "evidence": "|".join(best["ev"]) if best else "",
            "items": best["meta"].get("items") if best else None,
            "empty_state": best["meta"].get("empty_state") if best else None,
            "cms": cms, "via": via,
            "best_alt": [c["url"] for c in sorted(
                [x for x in cands if isinstance(x["score"], int) and x["score"] >= ESCALATE],
                key=lambda x: -x["score"])[:3]],
            "fetches": ag.used, "secs": round(time.time() - t0, 1),
            "notes_hint": "; ".join(note_bits),
            "home_ok": bool(home_html),
            "identity_ok": ident_ok,
            "not_found_reason": ident_ev if not ident_ok else "",
            "log": log[:6],
        }
        C.log_candidates(cands)
        C.checkpoint_write(rec)
        return rec


async def main():
    agencies = [a for a in C.load_agencies() if a["lane"] == "main"]
    done = C.checkpoint_read()
    todo = [a for a in agencies
            if str(a["idx"]) not in done or done[str(a["idx"])].get("tier") != 0]
    if "--only" in sys.argv:
        ids = set(sys.argv[sys.argv.index("--only") + 1].split(","))
        todo = [a for a in agencies if str(a["idx"]) in ids]
    print(f"tier0: {len(todo)} agencies to process ({len(agencies) - len(todo)} already checkpointed)")

    sem = asyncio.Semaphore(GLOBAL_CONC)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    async with httpx.AsyncClient(limits=limits, verify=True, http2=False) as c1, \
               httpx.AsyncClient(limits=limits, verify=False, http2=False) as c2:
        tasks = [asyncio.create_task(process(a, sem, c1, c2)) for a in todo]
        res = []
        for i, t in enumerate(asyncio.as_completed(tasks), 1):
            try:
                r = await t
            except Exception as e:
                print(f"  task failed: {type(e).__name__}: {e}")
                continue
            res.append(r)
            if i % 20 == 0:
                ok = sum(1 for x in res if x["status"] == "resolved")
                print(f"  {i}/{len(todo)} done, resolved so far {ok}")

    with open(C.OUT + "/tier0.log", "w", encoding="utf-8") as f:
        for r in sorted(res, key=lambda x: x["idx"]):
            f.write(f"{r['idx']:3} {r['status']:10} {str(r['score']):>4} "
                    f"{r['signal']:14} {r['rfp_url']}  {r['notes_hint']}\n")

    tot = len(res)
    ok = [r for r in res if r["status"] == "resolved"]
    esc = [r for r in res if r["status"] == "escalate"]
    un = [r for r in res if r["status"] == "unresolved"]
    print(f"\nTIER 0 RESULT  processed={tot}  resolved={len(ok)}  "
          f"escalate={len(esc)}  unresolved={len(un)}")
    by_sig = {}
    for r in ok:
        by_sig[r["signal"]] = by_sig.get(r["signal"], 0) + 1
    print("resolved by signal:", dict(sorted(by_sig.items(), key=lambda x: -x[1])))
    print("blocked/unreachable:", sum(1 for r in res if r["notes_hint"]))


if __name__ == "__main__":
    asyncio.run(main())
