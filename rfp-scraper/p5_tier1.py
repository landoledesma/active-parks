"""Phase 5 - Tier 1 rendered discovery.

Deterministic: driven from Python via subprocess, `--raw` everywhere, so the model
never reads a snapshot. 4 named sessions (w1..w4), reused across agencies.
Link text comes from textContent (innerText returns "" for collapsed nav).

Runs the same signal ladder against the rendered DOM, then VERIFY (identical
scorer as Tier 0) and a checkpoint line per agency.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlparse

import common as C

NODE = r"C:\Program Files\nodejs\node.exe"
CLI = r"C:\Users\teach\AppData\Roaming\npm\node_modules\@playwright\cli\playwright-cli.js"
SESSIONS = ["w1", "w2", "w3", "w4"]          # hard cap 4 (SETUP.md §6.4)
ACCEPT, ESCALATE = 60, 30
MAX_GOTO = 14                                 # per agency
SETTLE = 1.2                                  # seconds for JS listings to render

ENV = dict(os.environ)
ENV["MSYS_NO_PATHCONV"] = "1"
ENV["PATH"] = (r"C:\Users\teach\AppData\Roaming\npm;C:\Program Files\nodejs;"
               + ENV.get("PATH", ""))

LINKS_JS = """JSON.stringify({
 u: location.href,
 t: document.title || '',
 g: (document.querySelector('meta[name=generator]')||{}).content || '',
 n: document.querySelectorAll('a').length,
 L: [...document.querySelectorAll('a')].slice(0,1200).map(a =>
      [ (a.textContent||'').replace(/\\s+/g,' ').trim().slice(0,140), a.href ])
})"""

HTML_JS = """JSON.stringify({
 u: location.href,
 h: document.documentElement.outerHTML.slice(0, 500000)
})"""

RENDER_PROBES = ["/bids", "/bids.aspx", "/purchasing", "/procurement", "/rfp",
                 "/doing-business", "/business", "/legal-notices"]

# HTML site indexes list every page including the ones nav hides. Measured:
# greenwood.in.gov/a-z/ exposes 411 links vs 126 on the homepage.
INDEX_HUBS = ["/a-z", "/a-z/", "/sitemap", "/site-map", "/directory",
              "/how-do-i", "/departments"]

_print_lock = threading.Lock()


def cli(session: str, *args, timeout=75):
    """One playwright-cli invocation. shell=False -> no MSYS/quoting hazards."""
    cmd = [NODE, CLI, f"-s={session}", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=ENV,
                           cwd=C.ROOT)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        return -9, "", "timeout"
    except Exception as e:
        return -8, "", f"{type(e).__name__}: {e}"


def raw_json(session: str, js: str, timeout=75):
    rc, out, err = cli(session, "--raw", "eval", js, timeout=timeout)
    if rc != 0:
        return None
    s = out.strip()
    if not s:
        return None
    # --raw returns the evaluated value; unwrap if the harness quoted it
    for _ in range(2):
        try:
            v = json.loads(s)
        except Exception:
            m = re.search(r"[\{\[].*[\}\]]", s, re.S)
            if not m:
                return None
            s = m.group(0)
            continue
        if isinstance(v, str):
            s = v
            continue
        return v
    return None


def goto(session: str, url: str):
    rc, out, err = cli(session, "goto", url, timeout=90)
    return rc == 0


def rank_links(links, agency, site):
    """Score rendered anchors by lexicon; portals first, then lexicon paths."""
    out = []
    for t, h in links:
        if not h or not isinstance(h, str) or not h.startswith("http"):
            continue
        path = urlparse(h).path + "?" + (urlparse(h).query or "")
        blob = (t or "") + " " + path
        if C.NEG_LEX.search(blob):
            continue
        s = 0
        vend = C.portal_of(h)
        if vend and vend != "BoardDocs" and not C.same_org(h, agency["agency_domain"], site):
            s += 70
        if C.LEX_STRONG.search(t or ""):
            s += 40
        elif C.LEX_WEAK.search(t or ""):
            s += 18
        if C.PATH_LEX.search(path):
            s += 25
        if C.DETAIL_URL.search(path):
            s -= 60                                    # A4
        if s <= 0:
            continue
        s += min(C.depth(h), 4) * 2
        out.append((s, t or "", h))
    out.sort(key=lambda x: -x[0])
    ded, seen = [], set()
    for s, t, h in out:
        n = C.norm_url(h)
        if n in seen:
            continue
        seen.add(n)
        ded.append((s, t, h))
    return ded


def process(agency, session, cands):
    site = agency["agency_website"] or ("https://" + agency["agency_domain"])
    gotos = 0
    best = None

    def record(url, signal, score, ev, meta, link_text=""):
        nonlocal best
        dec = "accepted" if score >= ACCEPT else ("escalate" if score >= ESCALATE else "rejected")
        cands.append({
            "agency_name": agency["agency_name"], "agency_domain": agency["agency_domain"],
            "url": url, "tier": "1", "signal": signal, "score": score,
            "evidence": "|".join(ev) + f"|items={meta.get('items')}",
            "decision": dec, "note": (meta.get("title") or "")[:120]})
        cand = {"url": url, "signal": signal, "score": score, "ev": ev,
                "meta": meta, "link_text": link_text}
        if best is None or (score, C.depth(url)) > (best["score"], C.depth(best["url"])):
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

    # ---- homepage, rendered ----
    home = None
    for cand_url in [site, "https://" + C.host_of(site).lstrip("www."),
                     "http://" + C.host_of(site)]:
        gotos += 1
        if goto(session, cand_url):
            time.sleep(SETTLE)
            home = raw_json(session, LINKS_JS)
            if home and home.get("n", 0) > 0:
                break
        if gotos >= 3:
            break

    note_bits = []
    if not home:
        note_bits.append("browser could not load homepage")
        return {"idx": agency["idx"], "agency_name": agency["agency_name"], "tier": 1,
                "status": "unresolved", "rfp_url": "", "score": None, "signal": "",
                "evidence": "", "items": None, "empty_state": None, "cms": "",
                "via": "", "best_alt": [], "gotos": gotos,
                "notes_hint": "; ".join(note_bits), "home_ok": False}

    cms = home.get("g", "")
    base = home.get("u", site)
    ranked = rank_links(home.get("L", []), agency, site)

    # ---- 1. rendered nav links (this is what static scraping could not see) ----
    for s, t, h in ranked[:4]:
        visit(h, "rendered-link", t)
        if best and best["score"] >= ACCEPT and best["meta"].get("items", 0) >= 1:
            break

    # ---- 1b. HTML site index / A-Z hubs: they list what the nav hides ----
    if not best or best["score"] < ACCEPT:
        for p in INDEX_HUBS:
            if gotos >= MAX_GOTO - 3:
                break
            gotos += 1
            if not goto(session, urljoin(base, p)):
                continue
            time.sleep(SETTLE)
            d = raw_json(session, LINKS_JS)
            if not d or d.get("n", 0) < 20:
                continue                      # not a real index page
            for s, t, h in rank_links(d.get("L", []), agency, site)[:2]:
                visit(h, "site-index", t)
            if best and best["score"] >= ACCEPT:
                break

    # ---- 2. rendered probing of canonical paths (WAF-blocked sites reach here) ----
    if not best or best["score"] < ACCEPT:
        for p in RENDER_PROBES:
            if gotos >= MAX_GOTO - 1:
                break
            visit(urljoin(base, p), "rendered-probe")
            if best and best["score"] >= ACCEPT and best["meta"].get("items", 0) >= 1:
                break

    # ---- 3. second hop: follow lexicon links found on the best page so far ----
    if best and ESCALATE <= best["score"] < ACCEPT and gotos < MAX_GOTO:
        sub = rank_links(
            [(C.strip_html(m.group(2)), urljoin(best["url"], m.group(1)))
             for m in re.finditer(
                 r'(?is)<a\b[^>]*href=["\']([^"\'#][^"\']*)["\'][^>]*>(.*?)</a>',
                 best["meta"].get("html", ""))],
            agency, site)
        for s, t, h in sub[:2]:
            if C.norm_url(h) == C.norm_url(best["url"]):
                continue
            visit(h, "rendered-hop2", t)

    # ---- A3: self-hosted page that only points at a portal ----
    via = ""
    if best and best["score"] >= ACCEPT and best["meta"].get("items", 0) == 0 \
            and C.same_org(best["url"], agency["agency_domain"], site) and gotos < MAX_GOTO:
        for m in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\'#][^"\']*)["\']',
                             best["meta"].get("html", "")):
            u = urljoin(best["url"], m.group(1))
            vend = C.portal_of(u)
            if vend and vend != "BoardDocs" and not C.same_org(u, agency["agency_domain"], site):
                prev = best["url"]
                if visit(u, "portal-via-page") >= ACCEPT:
                    via = prev
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
            "cms": cms, "via": via,
            "best_alt": [c["url"] for c in cands[-8:]
                         if isinstance(c["score"], int) and c["score"] >= ESCALATE][:3],
            "gotos": gotos, "notes_hint": "; ".join(note_bits), "home_ok": True,
            "anchors": home.get("n")}


def worker(session, slice_, results):
    rc, out, err = cli(session, "open", "--browser=chrome", timeout=120)
    with _print_lock:
        print(f"  [{session}] opened rc={rc} ({len(slice_)} agencies)", flush=True)
    for i, a in enumerate(slice_, 1):
        cands = []
        try:
            rec = process(a, session, cands)
        except Exception as e:
            rec = {"idx": a["idx"], "agency_name": a["agency_name"], "tier": 1,
                   "status": "unresolved", "rfp_url": "", "score": None, "signal": "",
                   "evidence": "", "items": None, "empty_state": None, "cms": "",
                   "via": "", "best_alt": [], "gotos": 0,
                   "notes_hint": f"tier1 error {type(e).__name__}", "home_ok": False}
        with _print_lock:
            C.log_candidates(cands)
            C.checkpoint_write(rec)
            results.append(rec)
            if i % 5 == 0:
                print(f"  [{session}] {i}/{len(slice_)}", flush=True)
    cli(session, "close", timeout=60)


def main():
    agencies = {a["idx"]: a for a in C.load_agencies() if a["lane"] == "main"}
    done = C.checkpoint_read()
    todo = []
    for idx, a in agencies.items():
        r = done.get(str(idx))
        if r and 1 in (r.get("_tiers") or []):
            continue                                  # already through tier 1
        if r and r.get("status") == "resolved":
            continue                                  # tier 0 settled it
        todo.append(a)
    if "--only" in sys.argv:
        ids = set(sys.argv[sys.argv.index("--only") + 1].split(","))
        todo = [a for a in agencies.values() if str(a["idx"]) in ids]
    if "--limit" in sys.argv:
        todo = todo[:int(sys.argv[sys.argv.index("--limit") + 1])]

    print(f"tier1: {len(todo)} agencies over {len(SESSIONS)} sessions")
    slices = [todo[i::len(SESSIONS)] for i in range(len(SESSIONS))]
    results, threads = [], []
    t0 = time.time()
    for s, sl in zip(SESSIONS, slices):
        if not sl:
            continue
        th = threading.Thread(target=worker, args=(s, sl, results), daemon=False)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    cli("w1", "close-all", timeout=60)

    ok = [r for r in results if r["status"] == "resolved"]
    esc = [r for r in results if r["status"] == "escalate"]
    print(f"\nTIER 1 RESULT  processed={len(results)}  resolved={len(ok)}  "
          f"escalate={len(esc)}  unresolved={len(results)-len(ok)-len(esc)}  "
          f"({round(time.time()-t0)}s)")
    sig = {}
    for r in ok:
        sig[r["signal"]] = sig.get(r["signal"], 0) + 1
    print("resolved by signal:", dict(sorted(sig.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
