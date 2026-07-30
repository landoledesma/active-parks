"""Tier 4 - agentic navigation harness.

I drive the browser; this module does the bookkeeping so the caps and the log are
accurate rather than remembered:
  * every playwright-cli command is appended to out/tier4.log as it is issued
  * page loads are counted per agency and refused past MAX_LOADS
  * scoring is common.verify_score - the same scorer p7_adjudicate.py uses
  * identity is checked against the agency name + Indiana before anything counts

Subcommands (idx = agency index from out/tier4_sample.json):
  home <idx>                 open homepage; show nav/footer links + search form
  open <idx> <url>           open a page; VERIFY score + identity + what it says
  search <idx> <query>       use the site's own search form (action+field read
                             off the page, not guessed)
  links <idx> [regex]        list links on the CURRENT page matching regex
  find  <idx> <regex>        playwright-cli find --regex (Ctrl-F, 0 page loads)
  decide <idx> <status> <url> <score> <route> <reason...>
  status                     wall clock + per-agency loads
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse, urlencode

import common as C

NODE = r"C:\Program Files\nodejs\node.exe"
CLI = r"C:\Users\teach\AppData\Roaming\npm\node_modules\@playwright\cli\playwright-cli.js"
SESSIONS = ["w1", "w2", "w3", "w4"]
MAX_LOADS = 12
WALL_LIMIT = 35 * 60

LOG = C.OUT + "/tier4.log"
STATE = C.OUT + "/tier4_state.json"
SAMPLE = C.OUT + "/tier4_sample.json"
RESULTS = C.OUT + "/tier4_results.json"

ENV = dict(os.environ)
ENV["MSYS_NO_PATHCONV"] = "1"
ENV["PATH"] = (r"C:\Users\teach\AppData\Roaming\npm;C:\Program Files\nodejs;"
               + ENV.get("PATH", ""))


def sample():
    return {a["idx"]: a for a in json.load(open(SAMPLE, encoding="utf-8"))}


def state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    order = [a["idx"] for a in json.load(open(SAMPLE, encoding="utf-8"))]
    st = {"t0": time.time(),
          "sess": {str(i): SESSIONS[n % 4] for n, i in enumerate(order)},
          "loads": {}, "route": {}}
    json.dump(st, open(STATE, "w", encoding="utf-8"))
    return st


def save(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"))


def elapsed(st):
    return time.time() - st["t0"]


def log(txt):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(txt.rstrip() + "\n")


def loads(st, idx):
    return st["loads"].get(str(idx), 0)


def spend(st, idx, n=1):
    st["loads"][str(idx)] = loads(st, idx) + n
    save(st)


def cli(session, *args, timeout=70, count_as_load=False, idx=None, st=None):
    cmd = [NODE, CLI, f"-s={session}", *args]
    shown = "playwright-cli -s=%s %s" % (session, " ".join(
        f'"{a}"' if " " in a else a for a in args))
    log(f"    $ {shown}")
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=ENV, cwd=C.ROOT)
        out = p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        out = ""
        log("      -> TIMEOUT")
    if count_as_load and idx is not None and st is not None:
        spend(st, idx, 1)
    return out


def raw(session, js, **kw):
    out = cli(session, "--raw", "eval", js, **kw)
    s = (out or "").strip()
    for _ in range(3):
        if not s:
            return None
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


NAV_JS = """JSON.stringify({
 u:location.href, t:document.title||'',
 n:document.querySelectorAll('a').length,
 forms:[...document.querySelectorAll('form')].filter(f=>
    [...f.querySelectorAll('input')].some(i=>/search|query|keyword|^q$|^s$/i.test(i.name+' '+i.id+' '+i.type+' '+(i.placeholder||''))))
   .slice(0,3).map(f=>({action:f.getAttribute('action')||location.pathname,
     method:(f.getAttribute('method')||'get').toLowerCase(),
     field:([...f.querySelectorAll('input')].find(i=>/search|query|keyword|^q$|^s$/i.test(i.name+' '+i.id+' '+i.type+' '+(i.placeholder||'')))||{}).name||''})),
 L:[...document.querySelectorAll('a')].slice(0,700).map(a=>
    [(a.textContent||'').replace(/\\s+/g,' ').trim().slice(0,60), a.href])
})"""

PAGE_JS = """JSON.stringify({u:location.href,
 h:document.documentElement.outerHTML.slice(0,400000)})"""

INTEREST = re.compile(
    r"(bid|rfp|rfq|proposal|solicit|purchas|procure|quote|notice|business|vendor"
    r"|finance|contract|department|government|board|administration|doc)", re.I)
BORING = re.compile(
    r"(facebook|twitter|instagram|youtube|linkedin|^tel:|^mailto:|privacy|accessibility"
    r"|employment|job|career|calendar|lunch|menu|athletic|staff-director)", re.I)


def show_links(L, base, limit=28):
    seen, out = set(), []
    for t, h in L or []:
        if not h or not isinstance(h, str) or not h.startswith("http"):
            continue
        if BORING.search(h) or BORING.search(t or ""):
            continue
        blob = (t or "") + " " + urlparse(h).path
        if not INTEREST.search(blob):
            continue
        k = C.norm_url(h)
        if k in seen:
            continue
        seen.add(k)
        out.append((t or "", h))
    return out[:limit]


def identity(html, ag):
    ok_in, ev = C.indiana_identity(html)
    text = C.strip_html(html).lower()
    toks = [w for w in re.findall(r"[a-z]+", ag["agency_name"].lower())
            if len(w) > 3 and w not in {"city", "town", "school", "corp", "county",
                                        "community", "schools", "district"}]
    named = [w for w in toks if w in text]
    return ok_in, ev, f"name tokens {len(named)}/{len(toks)}"


def content_bits(html, n=4):
    pat = re.compile(r"(bid|rfp|rfq|proposal|quote|solicit|notice to|advertis|sealed)", re.I)
    blocks = re.findall(
        r"(?is)<(?:main|article|section|div|td|li|p|h1|h2|h3)[^>]*>(.*?)"
        r"</(?:main|article|section|div|td|li|p|h1|h2|h3)>", html or "")
    out = []
    for b in blocks:
        s = re.sub(r"\s+", " ", C.strip_html(b)).strip()
        if 20 < len(s) < 240 and pat.search(s) and s not in out:
            out.append(s)
        if len(out) >= n:
            break
    return out


def guard(st, idx):
    if elapsed(st) > WALL_LIMIT:
        print(f"!! WALL CLOCK {elapsed(st)/60:.1f} min - STOP")
        sys.exit(9)
    if loads(st, idx) >= MAX_LOADS:
        print(f"!! CAP: agency {idx} already used {MAX_LOADS} page loads")
        sys.exit(8)


def main():
    cmd = sys.argv[1]
    st = state()
    S = sample()

    if cmd == "status":
        print(f"elapsed {elapsed(st)/60:.1f} min / 35")
        tot = sum(st["loads"].values())
        print(f"loads total {tot}  agencies touched {len(st['loads'])}")
        print({k: v for k, v in sorted(st["loads"].items(), key=lambda x: int(x[0]))})
        return

    idx = int(sys.argv[2])
    ag = S[idx]
    sess = st["sess"][str(idx)]

    if cmd == "home":
        guard(st, idx)
        log(f"\n=== [{idx}] {ag['agency_name']} ({ag['agency_type']}) "
            f"{ag['agency_website']}  session={sess}")
        cli(sess, "goto", ag["agency_website"], count_as_load=True, idx=idx, st=st, timeout=90)
        time.sleep(1.0)
        d = raw(sess, NAV_JS)
        if not d:
            print("no DOM")
            log("      -> no DOM returned")
            return
        print(f"url={d['u']}\ntitle={d['t'][:80]}\nanchors={d['n']}")
        print(f"searchforms={d['forms']}")
        ls = show_links(d.get("L"), d["u"])
        print(f"interesting links ({len(ls)}):")
        for t, h in ls:
            print(f"   {t[:44]:46} {h[:96]}")
        log(f"      -> title={d['t'][:70]!r} anchors={d['n']} "
            f"searchforms={len(d['forms'])} interesting={len(ls)} "
            f"[loads {loads(st,idx)}/{MAX_LOADS}]")

    elif cmd == "open":
        guard(st, idx)
        url = sys.argv[3]
        cli(sess, "goto", url, count_as_load=True, idx=idx, st=st, timeout=90)
        time.sleep(1.2)
        d = raw(sess, PAGE_JS)
        if not d or not d.get("h"):
            print("no DOM")
            log(f"      -> {url} no DOM [loads {loads(st,idx)}]")
            return
        s, ev, meta = C.verify_score(d["u"], d["h"])
        ok_in, iev, nev = identity(d["h"], ag)
        bits = content_bits(d["h"])
        print(f"url={d['u']}\nVERIFY={s}  ev={'|'.join(ev)}  items={meta['items']}")
        print(f"title={meta['title'][:90]}")
        print(f"identity: indiana={ok_in} ({iev}) ; {nev}")
        for b in bits:
            print("   *", b[:190])
        if not bits:
            print("   (no bid-like content block)")
        log(f"      -> {d['u']} VERIFY={s} items={meta['items']} indiana={ok_in} "
            f"title={meta['title'][:60]!r} [loads {loads(st,idx)}/{MAX_LOADS}]")
        for b in bits[:2]:
            log(f"         content: {b[:150]}")

    elif cmd == "search":
        guard(st, idx)
        q = sys.argv[3]
        d = raw(sess, NAV_JS)
        if not d or not d.get("forms"):
            print("no search form on current page")
            log("      -> no search form on current page")
            return
        f = d["forms"][0]
        if not f.get("field"):
            print("search form has no named field")
            return
        action = urljoin(d["u"], f["action"] or "")
        url = action + ("&" if "?" in action else "?") + urlencode({f["field"]: q})
        print(f"submitting site search: {url}")
        cli(sess, "goto", url, count_as_load=True, idx=idx, st=st, timeout=90)
        time.sleep(1.2)
        d2 = raw(sess, NAV_JS)
        if not d2:
            print("no DOM")
            return
        print(f"url={d2['u']}  title={d2['t'][:70]}  anchors={d2['n']}")
        ls = show_links(d2.get("L"), d2["u"])
        for t, h in ls:
            print(f"   {t[:44]:46} {h[:96]}")
        log(f"      -> site-search q={q!r} -> {d2['u']} results={len(ls)} "
            f"[loads {loads(st,idx)}/{MAX_LOADS}]")

    elif cmd == "links":
        rx = sys.argv[3] if len(sys.argv) > 3 else None
        d = raw(sess, NAV_JS)
        if not d:
            print("no DOM")
            return
        pat = re.compile(rx, re.I) if rx else None
        n = 0
        for t, h in (d.get("L") or []):
            if not h or not h.startswith("http"):
                continue
            if pat and not (pat.search(t or "") or pat.search(h)):
                continue
            if not pat and not INTEREST.search((t or "") + h):
                continue
            print(f"   {t[:44]:46} {h[:100]}")
            n += 1
            if n >= 30:
                break
        log(f"      -> links filter={rx!r} shown={n} (0 loads)")

    elif cmd == "find":
        rx = sys.argv[3]
        out = cli(sess, "find", "--regex", rx)
        print(out[:1500])
        log(f"      -> find {rx!r} (0 loads)")

    elif cmd == "decide":
        status, url, score, route = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
        reason = " ".join(sys.argv[7:])
        res = {}
        if os.path.exists(RESULTS):
            res = json.load(open(RESULTS, encoding="utf-8"))
        res[str(idx)] = {"idx": idx, "agency_name": ag["agency_name"],
                         "agency_type": ag["agency_type"],
                         "agency_website": ag["agency_website"],
                         "status": status, "url": "" if url == "-" else url,
                         "score": None if score == "-" else int(score),
                         "page_loads": loads(st, idx),
                         "route": route, "reason": reason}
        json.dump(res, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log(f"    DECISION [{idx}] {ag['agency_name']}: {status} score={score} "
            f"route={route} loads={loads(st,idx)}\n      reason: {reason}")
        print(f"recorded {ag['agency_name']}: {status} ({loads(st,idx)} loads) "
              f"| elapsed {elapsed(st)/60:.1f}m")

    else:
        print("unknown command")


if __name__ == "__main__":
    main()
