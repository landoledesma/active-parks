# WRITE-UP — finding where 200 Indiana agencies post their RFPs

Every number below is derived from `out/debug_candidates.csv` (17,342 scored
candidates), `out/checkpoint.jsonl` and `out/rfp_pages.csv` via `p10_stats.py` —
not from recollection.

---

### 1. What got done

`out/rfp_pages.csv` — 200 rows, 8 columns, passing all five mandatory checks
(exactly 200 rows · the 4 input columns byte-identical to the XLSX · enum values
valid · every blank `rfp_url` carries a one-line reason · no duplicate `rfp_url`
across agencies).

**31 of 200 agencies (16%) have a verified RFP page** — 18 of 100 cities, 13 of
100 school districts. All 31 are `self-hosted`; hosting is decided by registrable
domain (A2), verified against both of the brief's own examples
(`urbandale.org/bids.aspx` → self-hosted, `sc-lancaster.civicplus.com` →
third-party). 8 of the 31 are CivicPlus-on-own-domain, correctly *not* counted as
third-party. 8 are empty listing pages ("no current bids"), kept as valid per A8.

The pipeline scored **17,342 candidates across 8,325 distinct URLs** for 190
agencies and accepted 172 of those scorings; every candidate, accepted or
rejected, is in `debug_candidates.csv` with its score, evidence and verdict.
What actually produced the 31 winners:

| Signal | Winners |
|---|---|
| `sitemap.xml` | 20 |
| site-search endpoint | 4 |
| canonical path probing | 2 |
| browser (rendered link / sitemap past a WAF) | 3 |
| Tier-3 agent read the page | 2 |

Sitemaps produced **two-thirds of all results**, confirming the ranking in
`SETUP.md` §8.4. Homepage link-scraping — revision 1's intended engine — produced
**zero**, exactly as the 10-agency sample predicted.

### 2. Assumptions and open questions

Assumptions A1–A9 in `PLAN.md` all held and are unchanged in substance. The ones
that did real work: **A3** (the portal wins over the middleman page — but its
*exception* fired in the one case we saw: Fishers' own listing page carries the
items, so it beat `fishersin.portal.opengov.com`); **A4** (listing pages only —
this rejected a 2016 Lawrence news post, a single Cicero advertisement and a
Monroe Central banking RFP); **A8** (empty listings are correct answers — 8 rows);
**A9** (no guessing an identity that cannot be anchored).

Open questions for you, in priority order:

1. **Row 177, Southport, is a wrong-entity row in the input.** `cityofsouthport.com`
   redirects to `cityofsouthport.gov` — the City of Southport, **North Carolina**
   (NC ZIP codes throughout). Is the input meant to contain this? I emit
   `not-found` with the mismatch stated rather than an NC bids page.
2. **Are the 10 blank `agency_website` rows intentional?** (unchanged from PLAN)
3. **Do you want "Public/Legal Notices" pages that carry only budget and tax
   notices?** This turned out to be the central judgement call — see §3.
4. Should `rfp_url` ever point at a board-agenda system (BoardDocs)? I excluded it.

### 3. The failure mode that cost the most

**Silent false negatives — failures that return HTTP 200.** Three of them, in
descending cost:

- **`Accept-Encoding: br` with no Brotli decoder.** I added a complete Chrome
  header set so WAFs would stop rejecting us for missing `Sec-Fetch-*` headers.
  That made us advertise Brotli, which `httpx` cannot decode unless the `brotli`
  package is installed — it was not. Every Brotli-serving site then returned
  **undecodable bytes with status 200**: empty titles, zero parsed items, scores
  collapsing to `path+20`. It read as "these agencies have no bids page" and
  corrupted an entire 190-agency sweep before I caught it by eyeballing page text.
  Exactly the class the `MSYS_NO_PATHCONV` trap in `SETUP.md` §5.1 belongs to: the
  command *succeeds*.
- **VERIFY rejecting the known-good page.** `cityoflawrence.org/procurement/bid-opportunities`
  — SETUP's own ground truth — scored **40** and would have been discarded, because
  its items are bare project names ("2025 CCMG Resurfacing Program") with no dates
  and no bid numbers. Fixed with a labelled-listing rule; it now scores 85.
- **A wrong eTLD+1.** Treating `in.gov` as a private domain collapsed all 31
  `*.in.gov` cities into one "organization", breaking the hosting rule and letting
  cross-agency links pass the same-org filter.

All three are recorded as R3.8, R3.1 and R3.4 in `PLAN.md`, and `test_verify.py`
now pins the scorer to 9 real pages (9/9 passing) so the calibration cannot
regress silently.

**The honest headline, though, is that 16% is a ceiling set by access and by
publication practice, not by scraper cleverness.** The 169 blanks break down as:

| Reason | Rows |
|---|---|
| WAF/403 blocked | 61 |
| Site reachable, no bid page found at all | 58 |
| Candidate found but failed VERIFY | 27 |
| Agent read the page: no solicitations published there | 12 |
| No website in the input (A9) | 10 |
| Input row points at the wrong agency | 1 |

*One row moved between the first two categories after the timebox.* La Porte was
recorded as "reachable, nothing found"; the post-timebox audit (R4) opened it in a
real browser and got a "Robot Challenge Screen" (sgcaptcha) with zero anchors. It is
a block the static client never flagged, so the row now reads *could not see* rather
than *does not publish* — a distinction that matters more than the count, because
one of them is a pending task and the other is a conclusion. **The blocked figure is
therefore a floor, not an exact count**; the same misdiagnosis may exist in other
rows that were never opened in a browser.

**Blocking is not passable honestly.** 2,139 of our requests were 403s and 316
were 429s. A6 prescribes escalating to a real browser — I tested that directly and
it *also* fails: `spencer.in.gov` and `cityofkokomo.org` return "403 Forbidden"
with zero anchors, `lebanon.in.gov` returns Cloudflare's "Attention Required!".
Passing those needs fingerprint evasion or a paid unblocker, both excluded by the
brief, so those rows are `not-found` with the block quoted in `notes`.

**Four separate attempts to buy recall with more requests each returned exactly
zero**, which is the most load-bearing result here:

| Attempt | Cost | New agencies resolved |
|---|---|---|
| Widened sitemap net (R3.13) | 113 extra URLs | **0** |
| Extrapolated winner URL shapes (R3.14) | 156 agencies × 159 paths, 890 s | **0** |
| Rendered re-verification of near-misses | 141 URLs in a real browser | **0** |
| Full Tier-0 re-probe under the *final* scorer | 158 agencies, ~7,000 new candidates | **0** |

The last one matters most: I re-ran the entire static ladder after every scoring
fix, on the suspicion that earlier sweeps had been judged by a worse scorer. It
produced ~7,000 additional scored candidates and moved the result **not at all**
(31 before, 31 after). Combined with the 8,876 hard 404s, the conclusion is not
"we needed a better guess" — it is that these pages are either behind a WAF or
not on the web at all.

### 4. What I'd do with more time

- **Ask, don't guess, about "Public Notices" pages.** The single biggest
  judgement call: an Indiana notices page is a *statutory* notices page, and the
  URL and title are identical whether it carries bid advertisements (accept — MSD
  Wayne Township, Union Co.) or only budget/tax notices (reject — Western Wayne,
  Lake Station, White River Valley). Scoring cannot separate them; only reading
  can. With a decision from you this becomes a rule rather than 17 hand calls.
- **Test the newspaper hypothesis.** Indiana law (IC 5-3-1) requires bid
  advertisement in a newspaper of record. For the 58 reachable sites with no bid
  page, the correct answer may genuinely be "they publish nowhere online". I would
  sample ~15 of them against county newspaper legal-notice archives to convert
  "not found" into "does not exist online", which is a far more useful answer.
- **Sample-measure the true miss rate.** Hand-check ~20 random unresolved but
  reachable agencies to put a confidence interval on recall, instead of leaving it
  unbounded.
- **~~Re-run Tier 0 under the final scorer~~ — done, and it changed nothing.** I
  listed this as the most likely source of extra recall, then ran it rather than
  leaving it as a suggestion: 158 agencies re-probed, ~7,000 new scored candidates,
  **0** new resolutions. Recorded here because a recommendation I did not test
  would have been worth less than a negative result I did.
- Politeness/throughput: Tier 0 averaged **45.9 fetches per agency** (the "8–15"
  estimate in PLAN was wrong, R3.6), and 8,876 of those were hard 404s. A
  per-domain adaptive budget — stop probing once a site 404s the first 6 canonical
  paths — would cut request volume by more than half at zero cost to yield.

### 5. Explicitly unresolved

- **169 of 200 rows have no `rfp_url`**, each with a stated reason (table above).
  I did not fill any of them with a plausible-looking guess.
- **8 of the 10 A9 agencies are unresolved.** Two domains were recovered by strict
  self-identification (Newburgh → `newburgh.in.gov`, Twin Lakes School Corp →
  `tlschools.org`), both flagged **"domain inferred — VERIFY MANUALLY"**; neither
  yielded a bid page. The strict check deliberately rejected `southeastern.org`
  for "Southeastern Career Center" and `southbend.net` for "South Bend Community
  School Corp" — both are different entities, and those are precisely the
  invisible errors the brief warns about.
- **3 of the 31 resolved rows were kept on an earlier verified capture** because
  the final re-check was blocked (403/429). They say so in `notes`. An earlier
  version of the adjudicator demoted them, which meant treating *my own* rate
  limiting as evidence against a page — a bug worth naming.
- **Zero third-party portals** among the resolved set. The classifier is
  demonstrably capable of it (unit-checked against the brief's examples); no
  resolved agency in this run publishes through an external portal.
- Tier 2 (XHR interception) was built and run over 141 candidates but **never
  changed an outcome** — no resolved page depended on it.
