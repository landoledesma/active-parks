# SETUP — prerequisites, environment & validation findings

Fully validated on this machine, 2026-07-29. Every row below was executed, not assumed.
Section "Validation run" records **what you should see** when re-running the checks.

---

## 1. Required stack — verified state

| Dependency | Required | Found | Status |
|---|---|---|---|
| Node.js | ≥18 | **v24.18.1** — `C:\Program Files\nodejs` | ⚠ installed, **not on PATH** |
| npm | — | **11.16.0**, global bin `C:\Users\teach\AppData\Roaming\npm` | ⚠ **not on PATH** |
| `@playwright/cli` | latest | **0.1.17** global — binary is **`playwright-cli`** | ✅ working |
| Playwright browsers | chromium | `chromium-1228` + `chromium_headless_shell-1228` in `%LOCALAPPDATA%\ms-playwright` | ✅ pre-installed, no download needed |
| uv | recent | **0.12.0** — `C:\Users\teach\.local\bin\uv.exe` | ✅ |
| Python | 3.11+ | system `python` = **Microsoft Store build 3.12.10** (sandboxed, unreliable) | ⚠ **do not use** — see §2 |
| git | any | 2.53.0 | ✅ |
| Playwright skill | registered | was **absent** from `~/.claude/skills` | ✅ **now installed** — see §4 |

---

## 2. Python environment — uv-managed venv (created)

The only Python on this machine is the Microsoft Store build, which uses filesystem
redirection for `AppData` writes and breaks venvs unpredictably. We therefore let uv
provision its **own** CPython, isolated from the system.

```bash
cd rfp-scraper
export PATH="/c/Users/teach/.local/bin:$PATH"
uv venv --python 3.13          # downloads managed CPython 3.13.14 (~21 MB, one time)
uv pip install httpx beautifulsoup4 lxml openpyxl
```

**Created and verified.** Interpreter: `.venv/Scripts/python.exe` → CPython **3.13.14**
(managed by uv, *not* the Store build).

Installed (12 packages resolved):

| Package | Version | Role |
|---|---|---|
| httpx | 0.28.1 | Tier-0 async HTTP |
| beautifulsoup4 | 4.15.0 | HTML parsing |
| lxml | 6.1.1 | fast parser backend for bs4 |
| openpyxl | 3.1.5 | read the XLSX input |
| + anyio, certifi, h11, httpcore, idna, soupsieve, et-xmlfile, typing-extensions | — | transitive |

Run scripts with the venv interpreter directly (no activation needed):

```bash
.venv/Scripts/python.exe scraper.py
# or
uv run --python .venv/Scripts/python.exe scraper.py
```

---

## 3. PATH fix — REQUIRED every session

Neither Node nor the npm global bin is on PATH in Claude Code's shell. Without this,
every `playwright-cli` call fails with "command not found".

**Git Bash (preferred — see §5 for why):**

```bash
export PATH="/c/Users/teach/AppData/Roaming/npm:/c/Program Files/nodejs:/c/Users/teach/.local/bin:$PATH"
export MSYS_NO_PATHCONV=1     # ← mandatory, see §5
```

**PowerShell:**

```powershell
$env:PATH = "C:\Users\teach\AppData\Roaming\npm;C:\Program Files\nodejs;C:\Users\teach\.local\bin;$env:PATH"
```

---

## 4. Playwright skill — registered

The skill ships **inside** the npm package and is **not** auto-registered with Claude Code.
Installed to the project:

```bash
mkdir -p .claude/skills/playwright-cli
cp -r "/c/Users/teach/AppData/Roaming/npm/node_modules/@playwright/cli/node_modules/playwright-core/lib/tools/cli-client/skill/." .claude/skills/playwright-cli/
```

Result — 10 files present:

```
.claude/skills/playwright-cli/SKILL.md
.claude/skills/playwright-cli/references/{element-attributes,playwright-tests,request-mocking,
  running-code,session-management,storage-state,test-generation,tracing,video-recording}.md
```

`SKILL.md` frontmatter declares `allowed-tools: Bash(playwright-cli:*) Bash(npx:*) Bash(npm:*)`.

**The skill is only needed for Tier 3** (agentic tail). Tiers 0-2 shell out to
`playwright-cli` from Python and need no skill.

---

## 5. Environment gotchas — all discovered by running, not by guessing

### 5.1 ⚠ MSYS path mangling silently corrupts regexes — **`MSYS_NO_PATHCONV=1` is mandatory**

Git Bash rewrites arguments that look like POSIX paths. A leading `/` in a regex gets
turned into a Windows path:

```bash
# BROKEN — Git Bash rewrote the regex into a filesystem path
$ playwright-cli find --regex "/bid|rfp|procure/i"
No matches found for /C:\/Program Files\/Git\/bid|rfp|procure\/i/.

# FIXED
$ MSYS_NO_PATHCONV=1 playwright-cli find --regex "/bid|rfp|procure|council/i"
Found 2 matches for /bid|rfp|procure|council/i: ...
```

This fails **silently as a false negative** — the command succeeds, just never matches.
Highest-risk gotcha in the whole setup.

### 5.2 `&` in URLs

Git Bash with quoting handles `&` fine (verified: `bidID=1&catID=2` survived intact).
PowerShell needs `--%`; cmd needs `^&`. **Prefer the Bash tool for playwright-cli.**

### 5.3 Binary bytes break `grep` on script output

Page text containing NUL bytes makes grep print `Binary file (standard input) matches`
and drop everything. Pipe through `tr -d '\000'` or redirect to a file first.

### 5.4 Other

- Binary is **`playwright-cli`**, not `playwright`. `npx playwright` resolves to a
  *different* package and prompts to download 1.62.0 — don't.
- Project path has spaces and non-ASCII (`Secretaría`) — always quote.
- `.venv/`, `out/`, `.playwright-cli/` are gitignored; keeping the venv out of OneDrive
  sync avoids churn (thousands of small files).
- `playwright-cli` writes snapshots/console logs to `.playwright-cli/` in the **cwd**.

---

## 6. Validation run — what you should see

All of the following were executed. Re-run before starting the pipeline.

### 6.1 Toolchain

```bash
export PATH="/c/Users/teach/AppData/Roaming/npm:/c/Program Files/nodejs:/c/Users/teach/.local/bin:$PATH"
node -v && npm -v && playwright-cli --version && uv --version
```
Expected: `v24.18.1` / `11.16.0` / `0.1.17` / `uv 0.12.0`

### 6.2 Python venv + input parse

```bash
.venv/Scripts/python.exe -c "import httpx,bs4,lxml,openpyxl,sys; print(sys.version)"
```
Expected: `3.13.14`, no ImportError.

### 6.3 Browser end-to-end (the Tier-1 workhorse)

```bash
export MSYS_NO_PATHCONV=1
playwright-cli -s=smoke open --browser=chrome
playwright-cli -s=smoke goto "https://www.greenwood.in.gov"
playwright-cli -s=smoke --raw eval "JSON.stringify({n:document.querySelectorAll('a').length,t:document.title})"
playwright-cli close-all
```
Expected: `{"n":254,"t":"Home | Greenwood, IN"}` (± site changes). Confirms
session + navigation + `--raw eval` JSON extraction all work.

### 6.4 Parallelism — 4 concurrent sessions

Verified: `w1..w4` opened, navigated and evaluated concurrently.
**Cold start for 4 sessions: ~46 s.** Steady-state per-page cost is far lower
(session reuse). RAM stayed acceptable. **Cap: 4 sessions** (`w1..w4`).
Cleanup with `playwright-cli close-all` (`kill-all` for zombies).

---

## 7. INPUT DATA FINDINGS (validated against the real file)

`assesment/ap-work-sample-INPUT.xlsx`

| Property | Value |
|---|---|
| Rows | 201 (1 header + **200** agencies) |
| Columns | 5 — `agency_name, state, agency_type, agency_website, agency_domain` (PDF says 4 + CSV) |
| Split | exactly **100 city** / **100 school district** |
| State | 100% `Indiana` |
| Duplicates | none (by name or domain) |
| `agency_domain` consistency | 100% — always a substring of `agency_website` |
| URL schemes | 190 `https`, 0 `http` |
| Trailing slashes | 85 of 190 (normalize for comparison, **emit verbatim** in output) |

### 🔴 10 agencies have NO website and NO domain (5% of the dataset)

Rows 7, 29, 34, 43, 57, 74, 110, 113, 129, 153 have only 3 populated cells:

```
Timothy L Johnson Academy · Newburgh · Greater Clark County Schools · Oakland City
Southeastern Career Center · Clay City · West Washington School Corp
Shelby Eastern Schools · Twin Lakes School Corp · South Bend Community School Corp
```

These have **no identity anchor**, so assumption A5 (never leave the given domain) cannot
apply. Handling is decided in `PLAN.md` §A9. This is the single most important input
finding — a pipeline that assumes 5 populated columns will crash or silently emit garbage.

---

## 8. 🔴 PIPELINE FINDINGS — these reshaped the architecture

Measured on a live 10-agency sample (5 cities + 5 school districts from the real input).

### 8.1 Homepage link-scraping is nearly worthless — **0 hits out of 10**

| Agency | HTML | anchors | lexicon hits | CMS |
|---|---|---|---|---|
| Waterloo | 111 kb | 102 | **0** | WordPress 7.0.2 |
| Frankfort | 19 kb | 42 | **0** | — |
| Lawrence | 91 kb | 281 | **0** | — |
| Greenwood | 46 kb | 126 | **0** | — |
| Spencer | 0 kb | 0 | — | **HTTP 403 (WAF)** |
| Western Wayne Schools | 233 kb | 264 | **0** | — |
| East Allen County Schools | 3 kb | 0 | **0** | (JS shell) |
| Edinburgh Community School Corp | 57 kb | 78 | **0** | Edlio CMS |
| North Vermillion Com Sch Corp | 46 kb | 13 | **0** | WordPress 7.0.2 |
| Porter Township School Corp | 91 kb | 89 | **0** | WordPress 6.8.6 |

The anchors are *present* — this is **not** a JS-rendering problem. The strings
`bid` / `rfp` / `procure` / `purchas` / `solicit` appear **literally zero times** in the
homepage HTML of Lawrence and Greenwood. Local-government homepages simply do not link to
their bids page from the front page; nav goes `Government / Departments / Do Business`.

**Depth-2 hub crawl also returned 0/10** — even for Lawrence, which demonstrably has a
procurement page. The bids link is 3+ levels deep or behind JS nav.

### 8.2 The PDF's own flagship example would have been missed

`urbandale.org` (cited in the PDF as `.../bids.aspx?bidID=318`) has **zero** bid links on
its homepage — but `https://www.urbandale.org/bids.aspx` returns 200,
title `"Bid Postings • Urbandale, IA • CivicEngage"`. Path probing found what link
scraping could not.

### 8.3 ✅ sitemap.xml is the highest-value signal

Lawrence's `sitemap.xml` (670 kb) yielded the *best* answer directly:

```
https://www.cityoflawrence.org/procurement
https://www.cityoflawrence.org/procurement/bid-opportunities   ← the actual listing page
https://www.cityoflawrence.org/news/2021/.../request-sealed-bids-...
```

Path probing found only `/procurement` (the parent). The sitemap found
`/procurement/bid-opportunities`, which is strictly better. One request, deep URLs, exact.

### 8.4 Corrected signal ranking (drives PLAN.md)

| Rank | Signal | Evidence |
|---|---|---|
| 1 | **sitemap.xml / sitemap index** | gave the best URL on the one site that had it |
| 2 | **canonical path probing** | found urbandale `/bids.aspx`, Lawrence `/procurement` |
| 3 | **site-search endpoint** (`/search?q=bids`) | untested, high expected value on CMSes |
| 4 | **rendered DOM (Playwright)** | needed for JS nav (East Allen: 3 kb shell) |
| 5 | homepage link extraction | **0/10 — demoted from primary to corroboration only** |

### 8.5 CMS fingerprinting works and is useful

`<meta name="generator">` gave WordPress 7.0.2 ×2, WordPress 6.8.6, Edlio CMS;
page titles expose CivicPlus (`CivicEngage`). Per-CMS path templates are worth having
(e.g. WordPress → `/?s=bids`, CivicPlus → `/bids.aspx`).

### 8.6 Confirmed failure modes in the wild

- **Spencer (`spencer.in.gov`)**: HTTP 403, 0 bytes → WAF. Predicted; escalate to Tier 1.
- **East Allen County Schools**: 3 kb / 0 anchors → JS shell, static fetch useless.
- **North Vermillion**: only 13 anchors → minimal site, likely thin content.

**Net effect:** the static tier is a *cheap filter*, not the main engine. Playwright moves
from "fallback" to "primary discovery for a large share of agencies" — the browser-first
instinct is validated by the data.
