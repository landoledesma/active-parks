"""Shared engine for the RFP page discovery pipeline (PLAN.md).

Holds: paths, lexicon, URL/domain helpers, the platform fingerprint dictionary,
the VERIFY content scorer, and the checkpoint / debug-log writers.
Every tier imports from here so scoring is identical across tiers.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from urllib.parse import urljoin, urlparse, urlunparse

# ---------------------------------------------------------------- paths

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "out")
os.makedirs(OUT, exist_ok=True)


def _find_input() -> str:
    names = ["ap-work-sample-INPUT.xlsx"]
    for base in (ROOT, os.path.dirname(ROOT)):
        for sub in ("assesment", "assessment", ""):
            for n in names:
                p = os.path.join(base, sub, n) if sub else os.path.join(base, n)
                if os.path.exists(p):
                    return p
    raise SystemExit("input xlsx not found")


INPUT_XLSX = _find_input()
AGENCIES_JSON = os.path.join(OUT, "agencies.json")
CHECKPOINT = os.path.join(OUT, "checkpoint.jsonl")
DEBUG_CSV = os.path.join(OUT, "debug_candidates.csv")
FINAL_CSV = os.path.join(OUT, "rfp_pages.csv")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- lexicon

# Positive: words that mean "this is where solicitations live".
LEX_STRONG = re.compile(
    r"(bid\s*(opportunit|posting|solicitation|notice|opening|tabulation)"
    r"|current\s+bids?|open\s+bids?|invitation\s+(to|for)\s+bids?"
    r"|request\s+for\s+(proposal|qualification|quot|bid|information)"
    r"|\brfps?\b|\brfqs?\b|\brfis?\b|\bitb\b|\bifb\b"
    r"|procurement|purchasing|solicitation)", re.I)

LEX_WEAK = re.compile(
    r"(\bbids?\b|\bbidding\b|doing\s+business|vendor|legal\s+notice|public\s+notice"
    r"|contract\s+opportunit|competitive\s+(bid|sealed))", re.I)

# URL-path lexicon.
PATH_LEX = re.compile(
    r"(bids?|rfps?|rfqs?|rfis?|itb|ifb|procure|procurement|purchas|solicit"
    r"|doing[-_/]?business|business[-_/]?opportunit|vendor|legal[-_/]?notice"
    r"|public[-_/]?notice|invitation[-_/]?to[-_/]?bid|request[-_/]?for[-_/]?proposal"
    r"|bid[-_/]?opportunit|contract[-_/]?opportunit)", re.I)

# Negative: pages that use the same words for something else.
NEG_LEX = re.compile(
    r"(employment|job[-_/s]|career|hiring|human[-_/]?resource|volunteer|internship"
    r"|scholarship|athletic|lunch|menu|enroll|registration[-_/]?form|staff[-_/]?direct"
    r"|alumni|donat|calendar|newsletter|obituar)", re.I)

# Empty-state phrasing: an empty listing page IS the right page (A8).
EMPTY_STATE = re.compile(
    r"(no\s+(current|open|active|pending)?\s*(bids?|rfps?|solicitations?|proposals?"
    r"|opportunit\w*|postings?|notices?)\s*(are\s+)?(available|posted|open|at\s+this\s+time|currently)?"
    r"|there\s+are\s+(currently\s+)?no\s+(open\s+|current\s+)?(bids?|rfps?|solicitations?|opportunit\w*)"
    r"|no\s+results\s+found"
    r"|check\s+back\s+(later|often|frequently)"
    r"|not?\s+bids?\s+at\s+this\s+time)", re.I)

# A listing introduced by a label, where the item titles are bare project names
# ("Current Opportunities: 2025 CCMG Resurfacing Program"). Measured on
# cityoflawrence.org/procurement/bid-opportunities, which is the correct page but
# scored 0 items: no dates, no bid numbers, no lexicon in the link text.
LISTING_LABEL = re.compile(
    r"((current|open|active|available|upcoming|pending|advertised)\s+"
    r"(bids?|rfps?|rfqs?|solicitations?|opportunit\w*|proposals?|projects?|contracts?"
    r"|postings?|invitations?)"
    r"|bids?\s+(currently\s+)?(out|available|advertised|being\s+accepted)"
    r"|accepting\s+(bids?|proposals?|quotes?))", re.I)

# Closing-date / status vocabulary.
DATE_WORDS = re.compile(
    r"(due\s+(date|by|on)?|closes?\b|closing|deadline|opens?\b|opening\s+date"
    r"|submission\s+deadline|bids?\s+due|proposals?\s+due|award(ed)?\b|posted\s+on)", re.I)

DATE_LITERAL = re.compile(
    r"("
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r")", re.I)

# Solicitation identifiers, e.g. "Bid #2024-07", "RFP No. 25-003".
BID_NUMBER = re.compile(
    r"\b(bid|rfp|rfq|rfi|itb|ifb|project|solicitation|contract)\s*"
    r"(#|no\.?|number|:)?\s*[#]?\s*\d{2,}[-/]?\d*", re.I)

# A4: single-solicitation / news-item detail pages are never the answer.
DETAIL_URL = re.compile(
    r"(/news/|/news$|/article|/post/|/blog/|/\d{4}/\d{2}/\d{2}/|/\d{4}/\d{2}/"
    r"|/press-release|/announcement|[?&](bid)?id=\d+|[?&]view=item|/item/\d+"
    r"|/detail|[?&]nid=\d+|[?&]recordid=)", re.I)

LOGIN_WALL = re.compile(
    r"(you\s+must\s+(log\s?in|register|sign\s?in)|please\s+(log\s?in|sign\s?in)"
    r"|log\s?in\s+to\s+(view|access|continue)|registration\s+is\s+required"
    r"|create\s+an\s+account\s+to)", re.I)

# ---------------------------------------------------------------- probing

CANONICAL_PATHS = [
    "/bids.aspx", "/bids", "/bid", "/rfp", "/rfps", "/purchasing", "/procurement",
    "/business/bids", "/doing-business", "/legal-notices", "/public-notices",
    "/finance/purchasing", "/departments/purchasing", "/vendor-opportunities",
    "/solicitations", "/requests-for-proposals",
    "/bid-opportunities", "/bidopportunities", "/rfp-bids", "/bids-rfps",
    "/bids-and-rfps", "/current-bids", "/open-bids", "/bid-postings",
    "/business/purchasing", "/government/purchasing", "/departments/finance/purchasing",
    "/about/bids", "/district/bids", "/departments/business-office",
    "/purchasing-bids", "/invitation-to-bid", "/requests-for-proposals-rfps",
    "/bids-and-proposals", "/rfp-rfq", "/bids-quotes", "/notices",
    "/business/bid-opportunities", "/services/bids", "/how-do-i/bids",
    "/departments/legal/bids", "/business", "/business-opportunities",
]

CMS_TEMPLATES = {
    "wordpress": ["/category/bids/", "/category/bids-rfps/", "/category/rfp/",
                  "/category/legal-notices/", "/category/public-notices/",
                  "/tag/bids/", "/bids-rfps/", "/bid-opportunities/",
                  "/?s=bids", "/?s=rfp"],
    "civicplus": ["/bids.aspx", "/Bids.aspx", "/BidOpen.aspx",
                  "/business/bids_and_rfps.php"],
    "edlio":     ["/apps/pages/index.jsp?uREC_ID=&type=d&pREC_ID=bids",
                  "/pf4/cms2/view_page?group_id=", "/bids", "/purchasing"],
    "finalsite": ["/departments/business-office", "/fs/pages/bids", "/bids",
                  "/district/business-office"],
    "apptegy":   ["/o/district/page/bids", "/page/bids", "/purchasing",
                  "/o/district/browse/bids"],
    "drupal":    ["/search/node?keys=bids", "/bids", "/rfp", "/taxonomy/term/bids"],
    "squarespace": ["/search?q=bids", "/bids"],
    "revize":    ["/bids", "/purchasing", "/rfp", "/bids-and-rfps"],
    "granicus":  ["/bids", "/purchasing"],
    "schoolwires": ["/Page/bids", "/domain/bids", "/site/default.aspx?PageID=bids"],
}

# eGov Strategies CMS: bids are a *document type* inside the Document Center,
# reachable only via a query id discovered from the type dropdown. Measured on
# greenwood.in.gov (type 42 = "Bids") and frankfort-in.gov (no such type).
EGOV_CENTER = "/egov/apps/document/center.egov"
EGOV_QUERY = "?view=search&eGov_searchType={}"

SEARCH_TEMPLATES = ["/?s=bids", "/search?q=bids", "/search?keywords=bids",
                    "/search/node?keys=bids", "/site-search?q=bids"]

# ---------------------------------------------------------------- portals

PORTALS = [
    (r"(^|\.)ionwave\.net$", "IonWave"),
    (r"(^|\.)planetbids\.com$", "PlanetBids"),
    (r"(^|\.)bonfirehub\.com$", "Bonfire"),
    (r"(^|\.)gobonfire\.com$", "Bonfire"),
    (r"(^|\.)civicplus\.com$", "CivicPlus"),
    (r"(^|\.)demandstar\.com$", "DemandStar"),
    (r"(^|\.)bidexpress\.com$", "BidExpress"),
    (r"(^|\.)procureware\.com$", "ProcureWare"),
    (r"(^|\.)opengov\.com$", "OpenGov"),
    (r"(^|\.)bidnetdirect\.com$", "BidNet Direct"),
    (r"(^|\.)publicpurchase\.com$", "Public Purchase"),
    (r"(^|\.)vendorregistry\.com$", "Vendor Registry"),
    (r"(^|\.)ebidexchange\.com$", "eBid eXchange"),
    (r"(^|\.)boarddocs\.com$", "BoardDocs"),
    (r"(^|\.)bidsandtenders\.\w+$", "Bids&Tenders"),
    (r"(^|\.)periscopeholdings\.com$", "Periscope"),
    (r"(^|\.)bidprime\.com$", "BidPrime"),
    (r"(^|\.)questcdn\.com$", "QuestCDN"),
    (r"(^|\.)equalis\w*\.com$", "Equalis"),
    # Only the state's own hosts - NOT every <city>.in.gov municipality.
    (r"^(www\.)?in\.gov$", "Indiana state portal (in.gov)"),
    (r"^(supplier|secure|procurement|idoa)\.in\.gov$", "Indiana state portal (IDOA)"),
    (r"(^|\.)doe\.in\.gov$", "Indiana DOE portal"),
    (r"(^|\.)indianaenterprisesystem\.com$", "Indiana state portal"),
    (r"(^|\.)supplier\.\w+$", "supplier portal"),
    (r"(^|\.)bidbuy\w*\.com$", "BidBuy"),
    (r"(^|\.)mypurchasing\w*\.com$", "MyPurchasing"),
    (r"(^|\.)e-builder\.net$", "e-Builder"),
    (r"(^|\.)smartprocure\.\w+$", "SmartProcure"),
    (r"(^|\.)bidlocker\w*\.com$", "BidLocker"),
    (r"(^|\.)bidnet\.com$", "BidNet"),
]

CMS_ON_OWN_DOMAIN = {  # CMS detected but hosting stays self-hosted (A2)
    "civicplus", "civicengage", "wordpress", "edlio", "finalsite", "apptegy",
    "drupal", "squarespace", "revize", "granicus", "municode", "schoolwires",
    "blackboard", "campussuite", "sitemaker",
}

HOSTING_SELF = "self-hosted"
HOSTING_THIRD = "third-party"
HOSTING_NONE = "not-found"

# ---------------------------------------------------------------- url utils


def norm_url(u: str) -> str:
    """Normalize for comparison only. Output columns keep the verbatim input."""
    if not u:
        return ""
    u = u.strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    netloc = p.netloc.lower()
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse((p.scheme.lower(), netloc, path, "", p.query, ""))


def host_of(u: str) -> str:
    try:
        return urlparse(u if re.match(r"^https?://", u, re.I) else "https://" + u).netloc.lower()
    except Exception:
        return ""


def registrable(hostname: str) -> str:
    """Good-enough eTLD+1 for .com/.org/.net/.gov/.edu/.us and *.k12.in.us."""
    h = (hostname or "").lower().lstrip(".")
    if h.startswith("www."):
        h = h[4:]
    parts = h.split(".")
    if len(parts) <= 2:
        return h
    # Multi-part public suffixes in this dataset. `in.gov` is critical: Indiana
    # gives every municipality <city>.in.gov, so without it all 31 in.gov cities
    # collapse into one "organization" and the A2 hosting rule breaks.
    for suf in ("k12.in.us", "in.us", "co.us", "ci.us", "lib.in.us",
                "in.gov", "state.in.us", "doe.in.gov",
                "org.uk", "co.uk", "gov.uk"):
        if h.endswith("." + suf):
            n = len(suf.split("."))
            return ".".join(parts[-(n + 1):])
    return ".".join(parts[-2:])


def same_org(url: str, agency_domain: str, agency_website: str = "") -> bool:
    """Domain-based hosting rule (A2): suffix match, subdomains OK."""
    h = registrable(host_of(url))
    for ref in (agency_domain, host_of(agency_website)):
        if not ref:
            continue
        r = registrable(ref if "." in ref else "")
        if r and (h == r or h.endswith("." + r) or r.endswith("." + h)):
            return True
    return False


def portal_of(url: str):
    h = host_of(url)
    for pat, name in PORTALS:
        if re.search(pat, h, re.I):
            return name
    return None


def classify(url: str, agency_domain: str, agency_website: str = "", cms: str = ""):
    """-> (rfp_hosting, rfp_platform, extra_note)"""
    if not url:
        return HOSTING_NONE, HOSTING_NONE, ""
    if same_org(url, agency_domain, agency_website):
        note = f"CMS: {cms}" if cms and cms.lower() in CMS_ON_OWN_DOMAIN else ""
        return HOSTING_SELF, HOSTING_SELF, note
    vendor = portal_of(url)
    if vendor:
        return HOSTING_THIRD, vendor, ""
    dom = registrable(host_of(url))
    return HOSTING_THIRD, dom, "unrecognized portal - verify vendor"


def depth(url: str) -> int:
    p = urlparse(url).path.strip("/")
    return 0 if not p else len(p.split("/"))


# ---------------------------------------------------------------- VERIFY


STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}


def indiana_identity(html: str):
    """Does this site place itself in Indiana? -> (ok, evidence)

    Guards the wrong-entity failure class the work sample is about. Real case in
    this dataset: the input gives Southport (Indiana) `cityofsouthport.com`, which
    redirects to `cityofsouthport.gov` — the City of Southport, NORTH CAROLINA.
    Emitting that city's bids page would be an invisible error.
    """
    text = strip_html(html or "")
    low = text.lower()
    if re.search(r"\bindiana\b", low) or re.search(r"\bin\s+4\d{4}\b", low) \
            or re.search(r",\s*in\s+4\d{4}", low):
        return True, "indiana"
    others = re.findall(r",\s*([A-Z]{2})\s+\d{5}", text)
    others = [s for s in others if s in STATE_ABBR and s != "IN"]
    if others:
        top = max(set(others), key=others.count)
        if others.count(top) >= 2:
            return False, f"self-identifies as {top} ({others.count(top)} addresses), not Indiana"
    return True, "no state marker found (not treated as a mismatch)"


def strip_html(html: str) -> str:
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
          .replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s)


def title_h1(html: str) -> str:
    t = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    hs = re.findall(r"(?is)<h1[^>]*>(.*?)</h1>", html or "")
    return strip_html((t.group(1) if t else "") + " " + " ".join(hs[:3]))


# Slug words that carry no specificity. Anything left over after removing these
# and the lexicon means the URL names ONE solicitation, not a listing.
GENERIC_SLUG = {
    "of", "for", "the", "to", "and", "a", "an", "in", "on", "at", "our", "all",
    "current", "open", "available", "upcoming", "new", "list", "listing", "lists",
    "page", "pages", "index", "home", "info", "information", "documents", "docs",
    "center", "centre", "online", "public", "city", "town", "county", "district",
    "school", "schools", "department", "departments", "office", "business",
    "doing", "services", "service", "government", "notice", "notices",
    "opportunities", "opportunity", "postings", "posting", "requests", "request",
    "proposals", "proposal", "quotes", "quote", "bidders", "bidder", "vendor",
    "vendors", "legal", "archive", "archives", "aspx", "php", "html", "htm",
    "corner", "board", "administration", "finance", "purchasing", "procurement",
    "bids", "bid", "rfp", "rfps", "rfq", "rfqs", "rfi", "itb", "ifb",
    "solicitation", "solicitations", "advertisement", "advertisements",
}


def slug_specificity(url: str) -> int:
    """How many content words the last path segment adds beyond generic ones.

    'bid-opportunities' -> 0 (a listing)
    'town-accepting-bids-on-sewer-project' -> 3 (ONE solicitation)
    """
    p = urlparse(url).path.rstrip("/")
    seg = p.rsplit("/", 1)[-1] if "/" in p else p
    seg = re.sub(r"\.(aspx|php|html?|jsp)$", "", seg, flags=re.I)
    words = [w for w in re.split(r"[-_.+]", seg.lower()) if w]
    return len([w for w in words if w not in GENERIC_SLUG and not w.isdigit()
                and len(w) > 2])


def count_items(text: str, html: str, detail: dict = None) -> int:
    """Solicitation-like items: an identifier, a bid phrase near a date, or a
    cluster of bid-titled links (a listing page is a list of links).

    `detail` is filled with the component counts, so callers can tell a real
    multi-entry listing from a single advertisement.
    """
    n = len(BID_NUMBER.findall(text))
    dated = 0
    rows = re.findall(r"(?is)<(?:tr|li)[^>]*>(.*?)</(?:tr|li)>", html or "")[:400]
    for r in rows:
        rt = strip_html(r)
        if len(rt) < 8:
            continue
        if (LEX_STRONG.search(rt) or LEX_WEAK.search(rt)) and \
           (DATE_LITERAL.search(rt) or DATE_WORDS.search(rt)):
            n += 1
            dated += 1
    # Link-cluster signal: anchors that name an INDIVIDUAL solicitation. Matching
    # the lexicon alone is not enough — on a site whose business is purchasing, the
    # nav itself matches everywhere (ciesc.org: a blog post counted 7 "items" from
    # its sidebar). A real listing links out to documents or dated entries.
    anchors = re.findall(r"(?is)<a\b([^>]*)>(.*?)</a>", html or "")[:600]
    titled = 0
    for attrs, a in anchors:
        at = strip_html(a).strip()
        if not (6 <= len(at) <= 200) or NEG_LEX.search(at):
            continue
        href = ""
        m = re.search(r'href=["\']([^"\']+)', attrs or "", re.I)
        if m:
            href = m.group(1)
        is_doc = bool(re.search(r"\.(pdf|docx?|xlsx?)(\?|$)", href, re.I))
        if BID_NUMBER.search(at) \
                or (is_doc and (LEX_STRONG.search(at) or LEX_WEAK.search(at))) \
                or (DATE_LITERAL.search(at) and (LEX_STRONG.search(at) or LEX_WEAK.search(at))):
            titled += 1
    if titled >= 3:
        n += titled
    if detail is not None:
        detail["dated_rows"] = dated
        detail["titled_links"] = titled
        detail["bid_numbers"] = len(BID_NUMBER.findall(text))
    return n


def verify_score(url: str, html: str, link_text: str = ""):
    """PLAN.md VERIFY table. -> (score, evidence:list[str], meta:dict)"""
    ev, score = [], 0
    text = strip_html(html)
    head = title_h1(html)
    path = urlparse(url).path + ("?" + urlparse(url).query if urlparse(url).query else "")

    if PATH_LEX.search(path) and not NEG_LEX.search(path):
        score += 20
        ev.append("path+20")
    if link_text and (LEX_STRONG.search(link_text) or LEX_WEAK.search(link_text)):
        score += 20
        ev.append("linktext+20")
    if LEX_STRONG.search(head) or LEX_WEAK.search(head):
        score += 20
        ev.append("title+20")

    comp = {}
    items = count_items(text, html, comp)
    empty = bool(EMPTY_STATE.search(text))
    n_li = len(re.findall(r"(?i)<li\b", html or ""))
    n_a = len(re.findall(r"(?i)<a\s", html or ""))
    labelled = bool(LISTING_LABEL.search(text)) and (n_li >= 2 or n_a >= 2)
    if labelled and items == 0:
        items = 2                      # a labelled list IS a listing (see LISTING_LABEL)

    if items >= 1:
        score += 25
        ev.append(f"items+25(n={items}{',labelled' if labelled else ''})")
    elif empty:
        score += 25
        ev.append("emptystate+25")

    # Corroborated intent: the URL path and the page's own title/h1 independently
    # say "this is the bids page". Two agreeing signals beat either alone, and it
    # rescues correct pages whose listing is rendered client-side.
    if PATH_LEX.search(path) and not NEG_LEX.search(path) and LEX_STRONG.search(head):
        score += 20
        ev.append("corroborated+20")

    if DATE_WORDS.search(text) and DATE_LITERAL.search(text):
        score += 10
        ev.append("dates+10")

    if LOGIN_WALL.search(text) and items == 0:
        score -= 30
        ev.append("loginwall-30")
    if NEG_LEX.search(head) and not LEX_STRONG.search(head):
        score -= 25
        ev.append("wrongtopic-25")
    # "Generic department page" means NO lexicon in the title at all. Requiring the
    # *strong* lexicon here double-punished correct pages: "Legal Notices" /
    # "Public Notices" earned title+20 from the weak lexicon and were then fined
    # -25 for not being strong, which is where most Indiana school corporations and
    # small towns actually advertise bids.
    if items == 0 and not empty and len(text) > 400 \
            and not LEX_STRONG.search(head) and not LEX_WEAK.search(head):
        score -= 25
        ev.append("generic-25")

    pdfs = re.findall(r"(?i)href=\"[^\"]+\.pdf\"", html or "")
    if items == 0 and not empty and len(pdfs) == 1:
        score -= 15
        ev.append("singlepdf-15")

    # A4: a single-solicitation / news detail page is never the deliverable.
    detail = bool(DETAIL_URL.search(path))
    if detail:
        score -= 40
        ev.append("detailpage-40")

    # A4, structural form: a URL slug that names ONE solicitation
    # ("town-accepting-bids-on-sewer-project") is an advertisement, not a listing.
    # Skipped when the page really does carry several dated entries.
    # A real listing proves itself with entries: >=2 dated rows or >=3 links whose
    # own text names a solicitation. Absent that, a specific slug means we are on
    # one advertisement (or a blog post *about* purchasing - ciesc.org scored 60
    # for "Coop Purchasing Gets Your School District More for Less").
    spec = slug_specificity(url)
    is_listing = comp.get("dated_rows", 0) >= 2 or comp.get("titled_links", 0) >= 3
    if spec and not is_listing:
        pen = 40 if spec >= 2 else 25
        score -= pen
        ev.append(f"specific-slug-{pen}(w={spec})")
        detail = True

    meta = {"items": items, "empty_state": empty, "title": head[:160],
            "textlen": len(text), "detail": detail,
            "dom_anchors": len(re.findall(r"(?i)<a\s", html or ""))}
    return score, ev, meta


# ---------------------------------------------------------------- io helpers

DEBUG_FIELDS = ["agency_name", "agency_domain", "url", "tier", "signal",
                "score", "evidence", "decision", "note"]


def log_candidates(rows):
    """Append scored candidates to out/debug_candidates.csv (traceability layer)."""
    if not rows:
        return
    new = not os.path.exists(DEBUG_CSV)
    with open(DEBUG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DEBUG_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in DEBUG_FIELDS})


def checkpoint_write(rec: dict):
    with open(CHECKPOINT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_RANK = {"resolved": 3, "escalate": 2, "unresolved": 1}


def checkpoint_read() -> dict:
    """Merge all checkpoint lines, keeping the BEST record per agency.

    Not "last line wins": a later re-run of one tier must never overwrite a
    better result an earlier tier already proved (this silently downgraded a
    resolved agency during development). `_tiers` records every tier that ran.
    """
    out = {}
    if not os.path.exists(CHECKPOINT):
        return out
    with open(CHECKPOINT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = str(r.get("idx"))
            prev = out.get(k)
            if prev is None:
                r["_tiers"] = [r.get("tier")]
                out[k] = r
                continue
            tiers = prev.get("_tiers", []) + [r.get("tier")]
            key_new = (_RANK.get(r.get("status"), 0), r.get("score") or -999)
            key_old = (_RANK.get(prev.get("status"), 0), prev.get("score") or -999)
            best = r if key_new > key_old else prev
            best["_tiers"] = tiers
            out[k] = best
    return out


def load_agencies() -> list:
    with open(AGENCIES_JSON, encoding="utf-8") as f:
        return json.load(f)


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)
