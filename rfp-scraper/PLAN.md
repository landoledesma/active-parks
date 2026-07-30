# PLAN — RFP Page Discovery Pipeline

**Status:** executed. Architecture below is **revision 3** — revision 1 assumed homepage
link-scraping would carry the load (measured **0/10**); revision 2 was validated on a
10-agency sample; revision 3 records what running all 190 changed. Read `SETUP.md` §8
first, then **"Revision 3"** at the bottom of this file for the corrections that matter.

Goal: for each of 200 Indiana agencies (100 cities, 100 school districts), find the page
where the agency posts RFPs / bids / solicitations, classify its hosting, produce one CSV.
The pipeline must generalize to any city / school district, not just these 200.

Input: `assesment/ap-work-sample-INPUT.xlsx` — 201 rows (200 agencies), 5 columns:
`agency_name, state, agency_type, agency_website, agency_domain`.
**10 agencies have blank website + domain** — see A9.

---

## Signal ranking (measured, not assumed)

This is the core of the design. Ordering comes from `SETUP.md` §8.4.

| # | Signal | Why it ranks here |
|---|---|---|
| 1 | **sitemap.xml** (+ sitemap index, `robots.txt` `Sitemap:`) | Only signal that produced the *best* URL unaided: `cityoflawrence.org/procurement/bid-opportunities`. One request, exact deep URLs. |
| 2 | **canonical path probing** | Found `urbandale.org/bids.aspx` (the PDF's own example, invisible to link-scraping) and Lawrence `/procurement`. |
| 3 | **site-search endpoint** — `/search?q=bids`, WP `/?s=bids` | CMSes expose it; cheap; high expected yield. |
| 4 | **rendered DOM via Playwright** | Required for JS nav / JS shells (East Allen: 3 kb, 0 anchors) and for WAF pages that a real browser passes legitimately. |
| 5 | **link extraction (home, then depth-2)** | **0/10 on homepages, 0/10 at depth 2.** Demoted: corroboration + discovery-path evidence only, never the primary signal. |

**Consequence:** the static tier is a cheap filter, not the engine. Playwright is
primary discovery for a large share of agencies, not a fallback.

---

## Architecture — tiered cascade with mandatory verification

Each tier receives only what the previous could not resolve. Every candidate URL from
any tier must pass VERIFY before entering the output.

```
Tier 0  Static discovery (httpx async + lxml/bs4)          all 200,   ~2-4 min
Tier 1  Rendered discovery (playwright-cli, 4 sessions)     T0 misses
Tier 2  Network interception (XHR/API portals)              SPA portals
Tier 3  Agentic tail (Claude + playwright-cli skill)        residual
VERIFY  Content scoring of every candidate                  always
```

### Tier 0 — static discovery

Per agency, in signal-rank order, stopping early on a high-confidence hit:

1. `robots.txt` → collect `Sitemap:` entries. `GET /sitemap.xml` (+ follow sitemap index,
   cap ~3 child sitemaps). Grep `<loc>` for the lexicon. **Prefer the deepest/most
   specific match** (`/procurement/bid-opportunities` beats `/procurement`).
2. Probe canonical paths (parallel, HEAD→GET):
   `/bids.aspx` `/bids` `/bid` `/rfp` `/rfps` `/purchasing` `/procurement`
   `/business/bids` `/doing-business` `/legal-notices` `/public-notices`
   `/finance/purchasing` `/departments/purchasing` `/vendor-opportunities`
   `/solicitations` `/requests-for-proposals`
3. CMS fingerprint via `<meta name="generator">` + title, then apply CMS-specific
   templates (WordPress → `/?s=bids`; CivicPlus/CivicEngage → `/bids.aspx`;
   Edlio / Finalsite / Apptegy → their known patterns).
4. Site-search endpoint with the lexicon.
5. Homepage + depth-2 link extraction — **last**, and mainly to record the discovery path
   and to catch outbound links to known portals (fingerprint dictionary below).

Politeness: global concurrency 20, **1 concurrent request per domain**, ~8-15 requests
per site (comparable to one human visit), 15 s timeout, 2 retries with backoff,
realistic browser User-Agent (compatibility with WAFs, **not** evasion).

### Tier 1 — rendered discovery (deterministic, model reads nothing)

Driven by the Python orchestrator via `subprocess`; `--raw` everywhere so output is JSON.

```bash
playwright-cli -s=w1 goto "https://example.gov"
playwright-cli -s=w1 --raw eval "JSON.stringify([...document.querySelectorAll('a')].map(a=>({t:(a.textContent||'').trim(),h:a.href})))"
```

Use `textContent`, **not** `innerText` — `innerText` returns "" for collapsed/hidden nav.
4 named sessions `w1..w4` (measured cold start ≈46 s for 4; reuse sessions across
agencies). Re-run the same signal ladder against the rendered DOM, plus expand JS menus
where cheap.

### Tier 2 — network interception (Bonfire, PlanetBids, OpenGov, …)

SPA portals render listings from XHR. When VERIFY sees an empty DOM on a portal URL:
`playwright-cli requests` → `response-body <n>` to confirm the listing API returns items.
More robust than DOM scraping, and impossible with plain BS4.

### Tier 3 — agentic tail (the only tier where the model navigates)

Residual hard cases: unusual nav, iframes, PDF-only publication, ambiguous candidates.
Claude uses the registered skill: `snapshot`, `find --regex` (the "Ctrl-F" idea), `click`.
Optionally 2-3 subagents, each with its **own** `-s=` session, partitioned by batch —
never exceed 4 browser sessions total. Every Tier-3 conclusion still runs VERIFY and
gets a `notes` entry.

### VERIFY — mandatory content scoring (the honesty layer)

Fetch the candidate (static first, rendered if the DOM looks empty) and score:

| Signal | Points |
|---|---|
| URL path matches lexicon | +20 |
| Link text / sitemap entry that led here matches lexicon | +20 |
| Page `<title>` or `<h1>` matches lexicon | +20 |
| ≥1 solicitation-like item (title + date/status/number) | +25 |
| Empty-state text ("no current bids at this time") | +25 — an empty listing page **is** the right page (A8) |
| Closing-date patterns (`due`, `closes`, `deadline`, date literals) | +10 |
| **Labelled listing** ("Current Opportunities:" + ≥2 items) | **+25** — added in execution, see R3.1 |
| **Corroborated intent** (path *and* title/h1 both match the strong lexicon) | **+20** — added in execution, see R3.2 |
| Penalties | login wall −30 · generic department page −25 · single PDF only −15 · **detail/news page −40 (R3.3)** |

Thresholds: **≥60 accept** · **30-59 → Tier 3** · **<30 reject** (kept in the debug log).
Prefer the deepest passing URL when several pass (Lawrence lesson:
`/procurement/bid-opportunities` > `/procurement`).

---

## Platform fingerprint dictionary

Hosting is decided **by domain, not by vendor**: if the final URL's registrable domain
matches `agency_domain` (suffix match, subdomains OK) → `self-hosted`; known portal
domain → `third-party` + vendor; other external domain → `third-party` + domain + note.

The PDF's own examples confirm this: `urbandale.org/bids.aspx` is *self-hosted* even
though it runs the CivicPlus CivicEngage CMS, while `sc-lancaster.civicplus.com` is
*third-party*. (Assumption A2. When CivicPlus CMS is detected on the agency's own domain,
`rfp_platform = self-hosted`, CMS recorded in `notes`.)

| Domain pattern | rfp_platform |
|---|---|
| `*.ionwave.net` | IonWave |
| `*.planetbids.com` | PlanetBids |
| `*.bonfirehub.com` | Bonfire |
| `*.civicplus.com` | CivicPlus |
| `demandstar.com` | DemandStar |
| `bidexpress.com` | BidExpress |
| `procureware.com` | ProcureWare |
| `procurement.opengov.com` | OpenGov |
| `bidnetdirect.com` | BidNet Direct |
| `publicpurchase.com` | Public Purchase |
| `vendorregistry.com` | Vendor Registry |
| `ebidexchange.com` | eBid eXchange |
| `*.boarddocs.com` | BoardDocs (board agendas — usually **not** a bid listing; flag) |
| Indiana state portal (`in.gov` procurement) | State portal (note which) |

Unrecognized external domain reached from the agency's site → `third-party`, platform =
the domain, note "unrecognized portal — verify vendor".

---

## Decisions & assumptions (all reappear in the write-up)

- **A1 — Input format.** PDF says CSV/4 columns; the file is XLSX/5. We use the XLSX and
  treat the bonus `agency_domain` as the identity anchor.
- **A2 — Hosting rule.** Domain-based, per the PDF's own examples (above).
- **A3 — Precedence (self-hosted page vs. the portal it links to).** `rfp_url` = **where
  the solicitations actually live.** If `/purchasing` merely links out to IonWave, the
  IonWave listing wins — that's the page the business must monitor; the middleman page
  goes stale. The discovery path is preserved in `notes` ("via {self-hosted URL}") so both
  survive in the deliverable. *Exception:* if the self-hosted page itself lists the
  solicitations and the portal only handles registration/downloads, the self-hosted page
  wins (primary publication point).
- **A4 — Granularity.** Listing page, never a single-bid detail page. The spec text
  ("the page that actually lists current solicitations") overrides the PDF's example URLs,
  which include `bidID=...` detail pages. Detail URLs are normalized upward.
- **A5 — Identity / no web search.** No search engines, ever. We never leave
  `agency_domain` except by following a link found on the agency's own site (or a
  fingerprinted portal URL). Chain of custody: home → link → portal. This eliminates the
  "Lawrence KS vs Lawrence IN" failure class by construction — and it worked: the sample
  run resolved the *Indiana* Lawrence correctly with zero disambiguation logic.
- **A6 — Bot blocking.** No evasion. Realistic UA, polite rates, bounded retries. On
  persistent 403/WAF (confirmed live on `spencer.in.gov`): escalate to Tier 1 (a real
  browser often passes passive checks legitimately); if still blocked → `not-found` +
  note `"blocked: HTTP 403 after N attempts"`. Dead/parked DNS → try https/http/www
  variants, then `not-found` + note.
- **A7 — `notes` column.** The PDF's rules reference a `notes` column the output table
  omits. We add it as an 8th column. Every `not-found` and every judgment call gets one line.
- **A8 — Empty listing pages are valid.** "No current bids at this time" on a bids page
  is a confirmed `rfp_url`, not a failure. A naive verifier would reject these.
- **A9 — The 10 agencies with no website (NEW, from validation).** 5% of the input has no
  `agency_website` and no `agency_domain`, so A5's identity anchor does not exist.
  **Decision: these are processed in a separate, explicitly labelled pass, and default to
  `not-found`.** We do *not* guess a homepage via search — that is precisely the invisible
  error the PDF warns about, and with no anchor there is nothing to verify against.
  Each gets `notes = "no agency_website in input; identity could not be anchored"`.
  *Optional stretch, only if time remains and clearly flagged:* attempt resolution
  requiring strong self-identification (site must name the exact agency **and** Indiana
  **and** be corroborated by an `.edu`/`.gov`/state directory), and mark every such row
  `notes = "website absent from input; domain inferred — VERIFY MANUALLY"`. Ask the
  hiring manager whether the blanks are intentional (this is a listed open question).

---

## Output

`out/rfp_pages.csv` — UTF-8, columns in this exact order:

```
agency_name, state, agency_type, agency_website   (verbatim from input, byte-identical)
rfp_url        listing page, or blank
rfp_hosting    self-hosted | third-party | not-found
rfp_platform   vendor | self-hosted | not-found
notes          one line: reason, discovery path, caveats (A7)
```

Side artifacts (not the deliverable, but they are the story):

- `out/debug_candidates.csv` — **every** candidate ever scored: agency, URL, tier, signal,
  score, evidence, accepted/rejected. This is the traceability layer, and the write-up
  writes itself from it.
- `out/checkpoint.jsonl` — one line per resolved agency, written incrementally.
  The pipeline is resumable; a crash at agency 150 costs nothing.

---

## Execution phases

*As built.* Script names are the executable record of the phases below.

| Phase | Script | Role |
|---|---|---|
| 2 | `p2_load.py` | loader + A9 split |
| 3 | `p3_tier0.py` | static ladder + inline VERIFY |
| 3b | `p3b_sitemap2.py` | widened sitemap net (**negative result**, R3.13) |
| 3c | `p3c_paths2.py` | evidence-derived path list (notices pages under varying parents) |
| 5 | `p5_tier1.py` | first rendered sweep (**retired**, R3.11) |
| 5b | `p5b_tier1b.py` | retargeted browser tier: sitemap/homepage past a WAF + block confirmation |
| 6 | `p6_reverify.py` | rendered re-verify + Tier-2 XHR portal confirmation |
| 7 | `p7_adjudicate.py` | **authoritative** re-VERIFY of every candidate; the only stage that demotes |
| 8 | `p8_a9.py` | A9 lane, strict self-identification |
| 9 | `p9_assemble.py` | CSV + validation gate |
| 10 | `p10_stats.py` | numbers for the write-up |
| — | `test_verify.py` | regression test pinning the scorer to real pages |

1. **Bootstrap** — `SETUP.md` §6 checks (PATH, `MSYS_NO_PATHCONV`, venv, browser smoke).
   ✅ already done and documented.
2. **Loader** — read XLSX, normalize 3-cell rows to 5, split the 10 anchor-less agencies
   into the A9 lane.
3. **Tier 0 sweep** over the 190 anchored agencies → checkpoint.
4. **VERIFY pass** on Tier-0 winners; demote failures.
5. **Tier 1 sweep** over unresolved (4 sessions) → VERIFY → checkpoint.
6. **Tier 2** for SPA-portal confirmation where needed.
7. **Tier 3** agentic tail on the residue; document every case.
8. **A9 lane** — emit `not-found` rows with notes (or the flagged stretch attempt).
9. **Assemble CSV** + sanity checks: exactly 200 rows; input columns byte-identical to
   source; enum values valid; every blank `rfp_url` has a note; no duplicate rfp_urls
   across different agencies (a duplicate usually means an identity bug).
10. **Write-up** — 5 bullets: what got done / assumptions + questions / worst failure mode
    / what's next.

---

## Constraints & considerations for the executing agent

- **PATH + MSYS**: every Bash call touching playwright-cli must first run
  `export PATH="/c/Users/teach/AppData/Roaming/npm:/c/Program Files/nodejs:$PATH"` **and**
  `export MSYS_NO_PATHCONV=1`. Without the second, `find --regex` fails as a *silent false
  negative* (`SETUP.md` §5.1). This is the highest-risk trap in the project.
- Binary is **`playwright-cli`**, not `playwright`.
- **Token discipline**: Tiers 0-2 are subprocess-driven — the model must **not** read
  snapshots there. `--raw` everywhere. `snapshot`/`find` are Tier-3 only.
- **`textContent`, not `innerText`** for link extraction (hidden nav returns "").
- **Python**: use `.venv/Scripts/python.exe` (uv-managed 3.13.14). Never the system
  `python` (Microsoft Store build).
- **Sessions**: hard cap 4 (`w1..w4`); `close-all` at the end, `kill-all` for zombies.
- **Checkpoint after every agency** — never hold results only in memory.
- **No WebSearch, no WebFetch of search engines** (A5). No paid scraping service — the
  point is an own, sustainable scraper. External cost: $0.
- **Output hygiene**: pipe script output through `tr -d '\000'` before grep (§5.3).
  Quote all paths (spaces + non-ASCII).
- The session transcript is a deliverable — keep the work visible and honest; failures get
  documented, not hidden.

---

## Failure-mode playbook

| Symptom | Action | Output |
|---|---|---|
| 403 / WAF (confirmed: `spencer.in.gov`) | escalate to Tier 1 real browser | still blocked → not-found + note |
| JS shell, ~0 anchors (confirmed: East Allen) | Tier 1 rendered DOM | normal flow |
| Dead DNS / parked | try https/http/www variants | not-found + note "site dead/parked" |
| Redirect to a different domain | follow, then **require the target to self-identify as Indiana** (R3.9) | note the new domain; on mismatch → not-found + "input points at the wrong agency" |
| Bids only in PDFs / newspaper legal notices | if a stable notices page exists use it, else not-found | note "PDF-only / legal-notice publication" |
| Email- or registration-only distribution | not-found | note "distributed by email/vendor registration" |
| Several plausible pages | highest VERIFY score; tie → the one with items; prefer deepest | note the runner-up |
| District posts via co-op / ESC | follow the on-site link (A5 holds) | third-party + note |
| No website in input (10 agencies) | A9 lane | not-found + note |

## Risks

- **CMS variance in school districts** (Edlio, Finalsite, Apptegy, WordPress) — mitigated
  by CMS templates + Tier 1 + Tier 3.
- **VERIFY false negatives on SPA portals** (empty DOM) — mitigated by Tier 2.
- **Low static yield** — measured; already priced into the design (Playwright is primary,
  not fallback).
- **Biggest expected time sink**: WAF/403 handling — bounded by the playbook (limited
  retries, then document and move on).

## Revision 3 — what execution changed, and why

Revision 2 was validated on a 10-agency sample. Running all 190 exposed six things
the sample could not. Each is a measurement, not a preference.

- **R3.1 — VERIFY missed correct pages whose items are bare project names.**
  `cityoflawrence.org/procurement/bid-opportunities` — the known-good URL from
  `SETUP.md` §8.3 — scored **40** and would have been *rejected*. Its listing reads
  "Current Opportunities: 2025 CCMG Resurfacing Program / 46th and Post I/I Removal
  …": no dates, no bid numbers, no lexicon in the link text, so `items = 0`. Added a
  **labelled-listing** rule (+25): a "Current/Open/Available <bids|opportunities>"
  label followed by ≥2 items *is* a listing. Lawrence now scores 85.
- **R3.2 — Corroborated intent (+20).** When the URL path *and* the page's own
  title/h1 independently say "bids", that is two agreeing signals and is enough to
  accept a page whose listing renders client-side. Prevents the 30-59 band from
  swallowing obviously-correct pages, which would have pushed ~170 agencies into the
  agentic tier — not affordable and not necessary.
- **R3.3 — A4 needed teeth in the scorer (−40).** The first sweep returned
  `cityoflawrence.org/news/2016/12/06/rfq-central-police-station-issued-city-lawrence`
  (score 75) over the real listing page. A4 already forbids detail pages in prose;
  the scoring table did not encode it. Detail/news URLs now take −40, and a bid-related
  detail page is mined for its category/tag archive instead ("normalize upward").
- **R3.4 — `in.gov` is a public suffix, and this broke the A2 hosting rule.** Indiana
  issues every municipality `<city>.in.gov`, so the naive eTLD+1 collapsed **31
  agencies into one "organization"**: any `*.in.gov` URL looked self-hosted for any
  `*.in.gov` agency, and the same-org filter let cross-agency links through (12
  agencies followed a link to `indianagps.doe.in.gov`). Fixed in `registrable()`;
  the state-portal fingerprint was also narrowed from `*.in.gov` to the state's own
  hosts. Verified against both of the PDF's examples: `urbandale.org/bids.aspx` →
  self-hosted, `sc-lancaster.civicplus.com` → third-party.
- **R3.5 — new signal: the eGov Strategies document centre.** Several Indiana cities
  (greenwood.in.gov, frankfort-in.gov, …) run a CMS where bids are not a *path* but a
  *document type* inside `/egov/apps/document/center.egov`, reachable only via an id
  discovered from a dropdown (Greenwood: type 42 = "Bids"). No path probe or link
  scrape can reach it. Also added: HTML site indexes (`/a-z`) as a Tier-1 hub —
  Greenwood's exposes 411 links against 126 on the homepage.
- **R3.6 — request budget: "8-15 per site" was wrong.** With sitemaps absent on most
  small-town sites, the ladder needs the full probe list; measured mean is **~49
  fetches/agency** in Tier 0. Budget raised to 55, still 1 request at a time per
  domain with 0.3 s spacing (≈1-2 req/s, one slow human). Tier 1 is capped at 14
  page loads per agency.
- **R3.8 — 🔴 the worst bug of the run: `Accept-Encoding: br` without a Brotli
  decoder.** Adding a complete Chrome header set (to stop WAFs 403-ing us for
  missing `Sec-Fetch-*`) made us advertise Brotli support. `httpx` only decodes
  `br` when the `brotli` package is installed — it was not. Every Brotli-serving
  site therefore returned **undecodable bytes with HTTP 200**: empty titles, zero
  items, scores collapsing to `path+20`. It looked exactly like "these sites have
  no bid pages", and it silently corrupted a whole 190-agency sweep. Fixed by
  installing `brotli`+`zstandard` and re-running from scratch. Lesson: claiming a
  content-encoding you cannot decode is the same class of silent false negative as
  the `MSYS_NO_PATHCONV` trap in `SETUP.md` §5.1 — the request *succeeds*.
- **R3.9 — 🔴 the input data contains a wrong-entity row, and the playbook told us
  to follow it.** Row 177, Southport (Indiana), gives
  `https://www.cityofsouthport.com`, which redirects to `cityofsouthport.gov` —
  the City of Southport, **North Carolina** (title "Southport NC", NC ZIP codes).
  The failure-mode playbook said "redirect to a different domain → follow;
  re-anchor identity to the redirect target", which would have emitted a North
  Carolina bids page for an Indiana agency: precisely the invisible error the
  brief warns about. **Changed:** re-anchoring now requires the redirect target to
  self-identify as Indiana (`indiana_identity()`); on failure the row is emitted
  as `not-found` with the mismatch spelled out in `notes`, and the discovery is
  abandoned rather than trusted. This is an input-data question for the hiring
  manager, not something a scraper should paper over.
- **R3.10 — the "generic department page −25" penalty was double-punishing correct
  pages.** It fired whenever the title lacked the *strong* lexicon, so a page
  titled "Legal Notices" or "Public Notices" earned `title+20` from the weak
  lexicon and was then fined −25 for not being strong. Those pages are where most
  small Indiana towns and school corporations actually advertise bids. The penalty
  now fires only when the title contains **no** lexicon term at all.
- **R3.11 — Tier 1 as specified did not earn its cost, and was retargeted.** The
  first implementation re-ran the whole ladder in a browser: 14 page loads per
  agency, ~50 minutes for 160 agencies, and only ~6 net conversions — because it
  was re-testing the same paths and the same (measured empty) homepage nav that
  Tier 0 had already tried. Replaced by `p5b_tier1b.py`, ≤7 loads per agency, whose
  job is only what a browser can do that `httpx` cannot: pull `sitemap.xml` and the
  homepage past a WAF, then hand them to the same ranking + VERIFY code.
- **R3.12 — 🔴 the real ceiling on this dataset is WAF blocking, and it is not
  passable honestly.** 67 of 190 agencies returned 403/429/503 to the static
  client. Escalating to a real Chrome — the remedy A6 prescribes — was tested
  directly and **also fails**: `spencer.in.gov` and `cityofkokomo.org` return
  "403 Forbidden" with 0 anchors, `lebanon.in.gov` returns Cloudflare's "Attention
  Required!" interstitial. Getting past these would require fingerprint evasion or
  a paid unblocking proxy, both of which the brief forbids. These rows are
  therefore emitted as `not-found` with the block quoted in `notes`, verified in a
  real browser. A correct empty answer, not a guess.
- **R3.14 — negative result: extrapolating the winners' URL shapes paid nothing
  either.** 11 of the first 27 resolutions were "public notices" / "legal notices"
  pages sitting under a parent that varies per site, often with underscores
  (`/school_board/public_notices`, `/central-office/public-notices`). Probing that
  inferred pattern — 156 agencies × up to 159 parent×leaf combinations, 890 s —
  resolved **0**. Conclusion: those URLs were *found* by sitemap and site-search,
  and they are not guessable. Path probing only pays on the genuinely canonical
  shapes (`/bids`, `/bids.aspx`, `/purchasing`); beyond that the combinatorics grow
  and the yield is zero. Two independent attempts to buy recall with more requests
  both returned nothing, which is itself the finding: **on this dataset recall is
  gated by access (WAFs) and by publication practice, not by cleverer guessing.**
- **R3.13 — negative result: widening the sitemap net paid nothing.** 52 agencies
  had a sitemap containing no lexicon match. Re-reading those sitemaps with a much
  broader pattern (business-office, clerk, treasurer, legal, document-centre, …)
  tried 113 extra URLs and resolved **0**. Recorded so nobody spends the time
  again: when a sitemap has no bid-ish URL, the bids page is generally not in the
  sitemap at all, rather than hiding under a synonym.
- **R3.7 — checkpoints merge by best-result, not last-write.** Re-running one tier
  over an agency another tier had already resolved silently *downgraded* it. The
  checkpoint reader now keeps the best record per agency and records every tier that
  ran, which is what makes the pipeline safely resumable.

## Open questions for the hiring manager

1. Are the 10 blank `agency_website` rows intentional (a test of how blanks are handled),
   or a data-export bug?
2. For an agency whose own page merely links to a portal, do you want the portal URL or
   the agency page? (We chose the portal — A3 — and keep the other in `notes`.)
3. Should `rfp_url` ever point at a board-agenda system (BoardDocs) when a district posts
   solicitations only inside board packets?

---

## Tier 4 — measured: what an agent adds on *healthy* sites (R4)

Tier 3 ran only on the 30-59 VERIFY band — the most ambiguous slice. This
experiment tests the agent where it should do best instead: sites that load
perfectly, are not WAF-blocked, have a website in the input, and where the
deterministic pipeline simply found nothing.

**Sample.** From `rfp_pages.csv`, blank `rfp_url` rows whose `notes` contain none
of `403 / 429 / blocked / cloudflare / no agency_website / identity could not /
verif` → **68 eligible**; sorted by `agency_name`, `random.seed(4)`, n=20
(`out/tier4_sample.json`). Deterministic baseline on these same 20 rows: **0**.
Deliverables were not touched; this lives in `out/tier4*`.

### R4.1 — Result: 0 conversions out of 20

| Metric | Value |
|---|---|
| Converted | **0 / 20** (baseline 0/20) |
| Outcomes | 19 `not-found`, 1 `blocked` |
| Wall clock | **12.8 min** of the 35-min cap |
| Page loads | **38 total, mean 1.9/agency** (cap 12; max used 3) |
| Cost per agency | ~38 s |

Not a single healthy-site agency in the sample had a solicitation listing the
deterministic tiers had missed. In several cases the agent proved the *site
itself* says there is nothing: Loogootee's search returns "No matching results",
Mitchell's "No results found", Poseyville's "Apologies, but the search returned
no results". This is the same conclusion as R3.13/R3.14 reached from the other
direction, and it is the third independent confirmation that recall here is
bounded by publication practice, not by technique.

**Extrapolation to all 68 rows:** ~44 min and ~130 page loads. With 0/20 observed,
the rule of three puts the 95% upper bound at ~15%, so the honest expectation for
the full 68 is **0-10 conversions**, most likely at the low end.

### R4.2 — Which navigation moves paid off (ranked)

This is the part that generalizes, ranked by *decisive evidence per page load*:

| Rank | Move | Cost | Verdict |
|---|---|---|---|
| 1 | **`find --regex` (Ctrl-F on the snapshot)** | **0 page loads** | Settled 6 of 8 agencies outright. Zero-cost disconfirmation; should run first, always. |
| 2 | **The site's own search box** | 1 load | 3 of 6 gave a server-rendered "no results" — the strongest negative available. Failed where results are JS-injected (Ligonier) or the field name is a decoy (Argos `search_paths[]`). |
| 3 | **eGov document-type enumeration** | 1 load + 0-load eval | Structurally decisive (see R4.3). |
| 4 | **Reading menus whose parent is `href="#"`** | 0 loads | Exposed a whole section link-scraping cannot see (Elwood). |
| 5 | **Hub pages** (Finance, Board of Works, Departments, Document Center) | 1 load each | ~8 opened, **all negative**. The intuitive move was the least productive one. |
| 6 | Footer | — | Nothing this sample. |

The ranking inverts the naive expectation: the cheapest moves (Ctrl-F, reading the
rendered menu) were the most informative, and the "obvious" move — clicking into
Finance/Purchasing hubs — paid nothing at all.

### R4.3 — What the agent found that probing and sitemaps structurally cannot

1. **Query-parameterised CMS views.** On the eGov CMS a bids listing lives at
   `…/document/center.egov?view=search&eGov_searchType=<id>`, and the id exists
   only inside a `<select>` on the page. Path probing cannot guess
   `?eGov_searchType=42`, and sitemaps never contain query-parameterised views.
   Reading the dropdown is the only route. **Concrete example:** Peru's document
   types are *Useful Links / Forms & Applications / Minutes / News & Notices /
   Agendas / Ordinances & Resolutions / Breaking News* — and Washington's are
   *Recent News / Useful Links / Forms / Agendas / Minutes / Ordinances / Meeting
   Audio / Breaking News*. **Neither has a "Bids" type**, which is why both are
   correctly empty — whereas the same move on Greenwood found type 42 = "Bids"
   (R3.5). The agent can prove *absence*, which a prober can only ever fail to find.
2. **Server-rendered "no results" as positive evidence of absence.** A crawler
   records a miss; the agent records the site *stating* it has nothing. That turns
   "not found" into "does not exist online" — a materially better answer for the
   `notes` column.
3. **Menus with no `href`.** Elwood's "Documents & Public Notices" submenu hangs
   off `href="#"`, so link extraction never sees its children ("Important
   Documents", "Meeting Agendas, Notices, Minutes & Videos"). Only the rendered
   menu exposes them. (Both children turned out to hold no solicitations.)
4. **A misdiagnosis in the deliverable.** La Porte (`cityoflaporte.com`) was
   recorded as "no bid page found", but real Chrome gets a **"Robot Challenge
   Screen"** (sgcaptcha) with 0 anchors. It is a WAF block the static pipeline
   never detected, so the 61-agency blocked count in `WRITEUP.md` is a *floor*,
   not an exact figure. Not circumvented (A6).

### R4.4 — Should the agentic share of the pipeline go up or down?

**Down for discovery; up, slightly, for audit — on the measurement, not taste.**

Agentic navigation converted **0/20 where the deterministic pipeline also got
0/20**, at ~38 s and 1.9 page loads per agency. There is no recall case for
expanding it: it found no page the cheap tiers missed. Two of its four genuinely
unique capabilities (R4.3) are *mechanisable* — dropdown enumeration on a
fingerprinted CMS and submitting the site's own search form are both deterministic
once you know to do them, and belong in Tier 0, not in an agent.

What does not mechanise is judgement about *what a page says* — the Tier-3
distinction between a notices page carrying bid ads and one carrying only budget
notices, and catching a misclassified WAF. So the defensible allocation is: keep
the agent as a thin, capped **audit layer** over a sample (it cost 13 minutes to
audit 20 agencies and corrected one row's diagnosis), and spend the engineering
budget on folding site-search submission and CMS dropdown enumeration into the
deterministic tier instead.
