# Active Parks — RFP Page Discovery Work Sample

Find, for each of **200 Indiana agencies** (100 cities + 100 school districts), the page
where that agency actually posts its RFPs / bids / solicitations, classify how that page
is hosted, and emit one CSV — without search engines, without paid unblockers, and
without guessing.

**Result: 31 of 200 agencies (16%) resolved to a verified listing page.** The other 169
rows are blank *with a stated reason*, which is the deliberate outcome of the design: a
wrong URL costs more than an empty one. The full reasoning, the negative results and the
open questions live in [`rfp-scraper/WRITEUP.md`](rfp-scraper/WRITEUP.md).

---

## Read these three, in this order

| # | Document | What it answers |
|---|---|---|
| 1 | [`rfp-scraper/SETUP.md`](rfp-scraper/SETUP.md) | Environment, toolchain, the input-data findings, and the 10-agency measurement that reshaped the architecture (§8). |
| 2 | [`rfp-scraper/PLAN.md`](rfp-scraper/PLAN.md) | The architecture: signal ranking, tiered cascade, the VERIFY scorer, assumptions A1–A9, and *Revision 3* — what running all 190 agencies changed. |
| 3 | [`rfp-scraper/WRITEUP.md`](rfp-scraper/WRITEUP.md) | The deliverable write-up: results, the failure mode that cost the most, what is explicitly unresolved. |

The one-line summary of the design: **the static tier is a cheap filter, not the engine.**
Homepage link-scraping — the intuitive approach — scored **0 out of 10** on a live sample
(`SETUP.md` §8.1). `sitemap.xml` produced two-thirds of every result that landed.

---

## File index

### Repository root

| Path | Type | What it is |
|---|---|---|
| `README.md` | doc | This index. |
| `.gitignore` | config | Excludes `.venv/`, `out/`, `.playwright-cli/`, `sessions/`, `.claude/`, caches. |
| `assesment/` | input | The assignment and its data (folder name kept as delivered). |
| `rfp-scraper/` | code | The pipeline, its docs and its scripts. |
| `sessions/` | local | Claude Code session transcripts — **untracked**, kept locally as the work record. |
| `.playwright-cli/` | local | Browser snapshots/console logs written by `playwright-cli` — **untracked**. |

### `assesment/` — the brief and its input

| File | What it is |
|---|---|
| `Work Sample_ Finding Where Agencies Post Their RFPs.pdf` | The assignment brief: task, output columns, the `urbandale.org` / `sc-lancaster.civicplus.com` hosting examples that anchor assumption A2. |
| `ap-work-sample-INPUT.xlsx` | The input: 201 rows (1 header + **200** agencies), 5 columns — `agency_name, state, agency_type, agency_website, agency_domain`. 100 city / 100 school district, 100% Indiana. **10 rows have no website and no domain** (the A9 lane) — see `SETUP.md` §7. |

### `rfp-scraper/` — documentation

| File | What it is |
|---|---|
| `PLAN.md` | Architecture and decision record. Signal ranking, the 4-tier cascade, the VERIFY scoring table and thresholds, the platform fingerprint dictionary, assumptions **A1–A9**, the failure-mode playbook, **Revision 3** (R3.1–R3.14, what execution corrected) and **Tier 4** (R4.1–R4.4, the measured value of agentic navigation). |
| `SETUP.md` | Prerequisites verified on this machine, the uv-managed Python 3.13 venv, the mandatory `PATH` + `MSYS_NO_PATHCONV` fix, environment gotchas, the validation run, the input-data findings (§7) and the **pipeline findings that reshaped the architecture** (§8). |
| `WRITEUP.md` | The write-up for the reviewer: what got done, assumptions and open questions, the failure mode that cost the most (silent HTTP-200 false negatives), the four attempts to buy recall that each returned zero, and what is explicitly unresolved. |

### `rfp-scraper/` — pipeline scripts

Run in numeric order. Each phase consumes what the previous one could not resolve, and
**every** candidate from every tier must pass the same VERIFY scorer in `common.py`.

| Phase | Script | Role | LOC |
|---|---|---|---|
| — | `common.py` | **Shared engine.** Paths, lexicon, URL/domain helpers, `registrable()` (the eTLD+1 fix for `in.gov`, R3.4), the platform fingerprint dictionary, the `verify_score()` content scorer, and the checkpoint / debug-log writers. Every tier imports it, so scoring is identical everywhere. | 581 |
| 2 | `p2_load.py` | Loader. Reads the XLSX verbatim, normalizes 3-cell rows to 5, splits the 10 anchor-less agencies into the A9 lane → `out/agencies.json`. Values are stored exactly as read so phase 9 can emit byte-identical columns. | 81 |
| 3 | `p3_tier0.py` | **Tier 0 — static discovery** (httpx async + lxml/bs4) with inline VERIFY. The full signal ladder: sitemap → portal outbound links → canonical path probing → CMS templates → site search → homepage/depth-2 links. 20 agencies in parallel, 1 request at a time per domain, 0.3 s spacing, hard per-agency fetch budget. | 523 |
| 3b | `p3b_sitemap2.py` | Widened sitemap net over the 52 agencies whose sitemap held no lexicon hit. **Negative result (R3.13): 113 extra URLs, 0 resolved.** Kept so nobody spends the time again. | 135 |
| 3c | `p3c_paths2.py` | Evidence-derived path probing — the "public/legal notices under a varying parent" shape that produced 11 of the first 27 wins. **Negative result (R3.14): 156 agencies × up to 159 combinations, 890 s, 0 resolved.** | 130 |
| 5 | `p5_tier1.py` | First rendered sweep — re-ran the whole ladder in a browser, 14 loads/agency. **Retired (R3.11):** ~50 min for ~6 net conversions, because it re-tested what Tier 0 had already tried. Kept as the record of why 5b exists. | 347 |
| 5b | `p5b_tier1b.py` | **Tier 1, retargeted.** Does only what a browser can do that `httpx` cannot: pull `sitemap.xml` and the homepage *past a WAF*, then hand them to the same ranking + VERIFY code. ≤7 loads/agency. Also confirms genuine blocks. | 225 |
| 6 | `p6_reverify.py` | Rendered re-verification of near-miss candidates (the JS-injected-listing false negative), plus **Tier 2**: XHR/API interception to confirm SPA portals whose DOM renders empty. Built, run over 141 candidates, **never changed an outcome**. | 162 |
| 7 | `p7_adjudicate.py` | **Authoritative re-VERIFY of every candidate any tier ever proposed**, under the final scorer — the only stage that *demotes*. Stage 1 static re-fetch, stage 2 rendered re-check for the 30–59 escalate band. Writes `out/final.jsonl`. | 277 |
| 7b | `p7b_tier3.py` | **Tier 3 — the agentic tail.** The 17 agencies in the 30–59 band, where scoring cannot decide: an Indiana "Public Notices" page carries bid ads (accept) or only budget/tax notices (reject) with an identical URL and title. Decisions recorded as data → `out/tier3_decisions.json`. | 81 |
| 8 | `p8_a9.py` | **The A9 lane** — the 10 agencies with no website and no domain. Generates candidate hostnames from the agency name (no external lookup) and accepts only on strong self-identification (names the agency **and** places it in Indiana). Every row so resolved is flagged *"domain inferred — VERIFY MANUALLY"*. `--probe` reports, `--commit` writes. | 236 |
| 9 | `p9_assemble.py` | Assembles `out/rfp_pages.csv` and runs the **five mandatory sanity checks**: exactly 200 rows · input columns byte-identical to the XLSX · valid enum values · every blank `rfp_url` carries a reason · no duplicate `rfp_url` across agencies. Hosting/platform are derived here so the A2 domain rule applies uniformly. | 261 |
| 10 | `p10_stats.py` | Derives every number `WRITEUP.md` quotes from the artifacts rather than from memory. | 87 |
| — | `t4.py` | **Tier 4 harness** — bookkeeping for the agentic-navigation experiment (R4): logs every `playwright-cli` command, enforces the per-agency page-load cap, scores with the same `verify_score`, checks identity. Subcommands: `home`, `open`, `search`, `links`, `find`, `decide`, `status`. | 339 |
| — | `test_verify.py` | **Regression test pinning the scorer to 9 real pages** — every case is a misjudgement that actually happened (a co-op purchasing *blog post* that scored 95; a single Cicero *advertisement*; the Lawrence ground truth that scored 40). 9/9 passing. Run: `.venv/Scripts/python.exe test_verify.py`. | 69 |

### `rfp-scraper/out/` — artifacts (gitignored, regenerated by running the pipeline)

| File | What it is |
|---|---|
| `rfp_pages.csv` | **The deliverable.** 200 rows × 8 columns: `agency_name, state, agency_type, agency_website, rfp_url, rfp_hosting, rfp_platform, notes`. The first four are byte-identical to the input. |
| `debug_candidates.csv` | **The traceability layer** — all **17,342** candidates ever scored across 8,325 distinct URLs, with tier, signal, score, evidence and accept/reject verdict. The write-up is derived from this file. |
| `checkpoint.jsonl` | One line per resolved agency, written incrementally; the merge keeps the **best** record per agency (R3.7), which is what makes the pipeline safely resumable. |
| `final.jsonl` | Phase 7's authoritative adjudication — what phase 9 actually reads. |
| `agencies.json` | Phase 2's verbatim copy of the input, plus the A9 split. |
| `tier3_decisions.json` | The 17 hand-read Tier-3 calls with their evidence. |
| `a9_inferred.json` | The A9 lane's inferred-hostname attempts and their self-identification verdicts. |
| `tier4_sample.json`, `tier4_results.json`, `tier4_state.json`, `tier4.log` | The Tier-4 experiment: the seeded 20-agency sample, outcomes, load counters and the full command log. |
| `tier0.log`, `tier1.log`, `tier1b.log`, `tier2.log`, `adjudicate2.log` | Per-phase run logs. |

---

## Running it

Full prerequisites and the validation run are in `SETUP.md`. The short version:

```bash
cd rfp-scraper

# PATH fix — required every session (Git Bash)
export PATH="/c/Users/teach/AppData/Roaming/npm:/c/Program Files/nodejs:/c/Users/teach/.local/bin:$PATH"
export MSYS_NO_PATHCONV=1        # mandatory — see SETUP.md §5.1

# Python env (uv-managed CPython 3.13, never the Microsoft Store build)
uv venv --python 3.13
uv pip install httpx beautifulsoup4 lxml openpyxl brotli zstandard

# Pipeline
.venv/Scripts/python.exe p2_load.py
.venv/Scripts/python.exe p3_tier0.py
.venv/Scripts/python.exe p3b_sitemap2.py
.venv/Scripts/python.exe p3c_paths2.py
.venv/Scripts/python.exe p5b_tier1b.py
.venv/Scripts/python.exe p6_reverify.py
.venv/Scripts/python.exe p7_adjudicate.py
.venv/Scripts/python.exe p7b_tier3.py
.venv/Scripts/python.exe p8_a9.py --commit
.venv/Scripts/python.exe p9_assemble.py      # writes out/rfp_pages.csv + sanity gate
.venv/Scripts/python.exe p10_stats.py        # the write-up's numbers

# Regression test for the scorer
.venv/Scripts/python.exe test_verify.py
```

Two traps that fail **silently, as HTTP 200 / exit 0** — both are documented because both
actually bit:

- `MSYS_NO_PATHCONV=1` — without it Git Bash rewrites a leading `/` in a regex into a
  Windows path and `find --regex` never matches (`SETUP.md` §5.1).
- `brotli` + `zstandard` must be installed — advertising an encoding you cannot decode
  returns undecodable bytes with status 200, which reads as "this agency has no bids
  page" (`PLAN.md` R3.8). It corrupted an entire 190-agency sweep.

---

## Results at a glance

**31 resolved** — all `self-hosted`, 18 cities and 13 school districts. What produced them:

| Signal | Winners |
|---|---|
| `sitemap.xml` | 20 |
| Site-search endpoint | 4 |
| Browser (rendered link / sitemap past a WAF) | 3 |
| Canonical path probing | 2 |
| Tier 3 (agent read the page) | 2 |
| Homepage link-scraping | **0** |

**169 blank**, each with a reason:

| Reason | Rows |
|---|---|
| WAF / 403 blocked | 61 |
| Site reachable, no bid page found | 58 |
| Candidate found but failed VERIFY | 27 |
| Agent read the page: nothing published there | 12 |
| No website in the input (A9) | 10 |
| Input row points at the wrong agency | 1 |

The ceiling here is **access and publication practice, not scraper cleverness**. Four
independent attempts to buy recall with more requests — a widened sitemap net, extrapolated
URL shapes, rendered re-verification, and a full Tier-0 re-probe under the final scorer
(~7,000 fresh candidates) — returned **zero new agencies each**. See `WRITEUP.md` §3.
