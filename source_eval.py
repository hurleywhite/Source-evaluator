#!/usr/bin/env python3
"""
Source Evaluator v2 (HSUS 0–100)

What it does:
- Takes Works Cited text (any format) or URLs.
- Fetches each URL + optional site policy pages (about/corrections/etc.).
- Separates "fetchability" from "credibility" (paywall/bot-block != junk).
- Applies gating rules (satire, obvious spam, unretrievable).
- Scores C1–C10 (0–2 each), total 0–20 => HSUS 0–100.
- Supports intended use (A/B/C) and claim relationship (self/third_party/etc.).
- Optional LLM scoring that is evidence-bound and quote-validated.

Important design choices:
- Minimal-text / no-outbound-links are NOT auto-rejects (they reduce evidence/method scores).
- Official/state sources are not automatically "bad"; conflict-of-interest is claim-relative.
- LLM output is accepted ONLY if it supplies quotes that exist in the evidence pack and passes validation.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import requests
import tldextract
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dateutil import parser as dateparser

# Optional: readability for cleaner main text
try:
    from readability import Document
    HAS_READABILITY = True
except Exception:
    HAS_READABILITY = False

# Optional: PDF extraction
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
    HAS_PDFMINER = True
except Exception:
    HAS_PDFMINER = False

# Optional: LLM scoring (OpenAI SDK)
try:
    from openai import OpenAI
    HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# -----------------------------
# Configuration
# -----------------------------

DEFAULT_TIMEOUT = 25
USER_AGENT = "SourceEvaluatorBot/2.0 (+contact: research@yourcompany.example)"
SLEEP_BETWEEN_REQUESTS_S = 0.4

CRAWL_PATHS = [
    "/about", "/about-us", "/contact", "/contact-us",
    "/editorial-policy", "/ethics", "/standards", "/methods", "/methodology",
    "/corrections", "/correction", "/retractions",
    "/terms", "/privacy", "/policies"
]

SATIRE_KEYWORDS = ["satire", "parody", "humor", "humour", "comedy", "entertainment"]

KNOWN_SATIRE_DOMAINS = {
    "theonion.com",
    "babylonbee.com",
    "clickhole.com",
}

# Local hard blocklist (optional)
KNOWN_BAD_DOMAINS = set()

DOMAIN_REGISTRY_PATH = "domain_registry.json"

# Simple paywall / bot-block heuristics (non-exhaustive)
PAYWALL_HINTS = [
    "subscribe to continue", "subscribe now", "sign in to continue",
    "enable cookies", "enable javascript", "your browser is not supported",
    "access denied", "unusual traffic", "verify you are human", "captcha"
]

# -----------------------------
# Data models
# -----------------------------

@dataclass
class FetchedDoc:
    url: str
    final_url: str
    status_code: int
    fetch_status: str  # ok|http_error|timeout|blocked|paywall|pdf|pdf_no_parser|xml|unknown
    content_type: str
    bytes_downloaded: int
    html: str = ""
    text: str = ""
    title: str = ""
    author: str = ""
    published_date: str = ""
    site_name: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    error: str = ""

@dataclass
class Criterion:
    score: int
    reason: str
    evidence_quotes: List[str] = field(default_factory=list)

@dataclass
class SourceResult:
    url: str
    final_url: str
    domain: str
    group_label: str  # optional label parsed from Works Cited line (e.g., HRF / Non-HRF / etc.)
    intended_use: str  # A/B/C
    relation: str      # self|adversary|third_party|non_political_fact|unknown
    fetch_status: str
    content_type: str
    bytes_downloaded: int

    gating: Dict[str, Any]  # {auto_reject, auto_restrict, reasons, restrict_reason, warnings}
    criteria: Dict[str, Criterion]
    total_0_20: int
    hsus_0_100: int
    recommendation: str
    works_cited_entry: str
    evidence_pages: List[str]
    llm_used: bool
    llm_error: str = ""


# -----------------------------
# Utilities
# -----------------------------

def now_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def normalize_url(url: str) -> str:
    url = url.strip().strip(").,;\"'")
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
    return url

def safe_filename(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def get_registered_domain(url: str) -> str:
    ext = tldextract.extract(url)
    if not ext.domain or not ext.suffix:
        return urlparse(url).netloc.lower()
    return f"{ext.domain}.{ext.suffix}".lower()

def extract_urls_from_text(text: str) -> List[str]:
    urls = set(re.findall(r'https?://[^\s)>\]]+', text))
    bare = re.findall(r'\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s)>\]]*)?', text)
    for b in bare:
        if b.startswith("http"):
            continue
        urls.add("https://" + b)
    cleaned = []
    for u in urls:
        nu = normalize_url(u)
        if nu:
            cleaned.append(nu)
    return sorted(set(cleaned))

def load_domain_registry(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def requests_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    })
    return s

def clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[:n]

def normalize_date(s: str) -> str:
    if not s:
        return ""
    try:
        dt = dateparser.parse(s)
        if not dt:
            return ""
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""

def extract_meta(soup: BeautifulSoup) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for tag in soup.find_all("meta"):
        if tag.get("property") and tag.get("content"):
            meta[tag["property"].strip().lower()] = tag["content"].strip()
        if tag.get("name") and tag.get("content"):
            meta[tag["name"].strip().lower()] = tag["content"].strip()
    return meta

def sanitize_html(html: str) -> str:
    if not html:
        return ""
    html = html.replace("\x00", "")
    html = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", html)
    return html

def detect_paywall_or_block(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in PAYWALL_HINTS)

def looks_like_xml(content_type: str, text: str) -> bool:
    if "xml" in (content_type or "").lower():
        return True
    t = (text or "").lstrip()
    return t.startswith("<?xml") or t.startswith("<rss") or t.startswith("<feed")

def format_works_cited(doc: FetchedDoc, accessed: str) -> str:
    author = (doc.author or "").strip() or "Unknown author"
    title = (doc.title or "").strip() or "Untitled"
    site = (doc.site_name or "").strip() or get_registered_domain(doc.final_url or doc.url)
    date = (doc.published_date or "").strip() or "n.d."
    url = doc.final_url or doc.url
    return f'{author}. "{title}." {site}, {date}. {url} (accessed {accessed}).'


# -----------------------------
# Fetching + parsing
# -----------------------------

def fetch_doc(session: requests.Session, url: str, cache_dir: str) -> FetchedDoc:
    os.makedirs(cache_dir, exist_ok=True)
    cache_key = safe_filename(url)
    cache_path = os.path.join(cache_dir, f"{cache_key}.json")

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return FetchedDoc(**data)
        except (json.JSONDecodeError, TypeError, ValueError):
            # Corrupt/empty cache; delete and refetch
            try:
                os.remove(cache_path)
            except OSError:
                pass

    final_url = url
    status = 0
    content_type = ""
    raw = b""
    fetch_status = "unknown"
    error = ""

    try:
        time.sleep(SLEEP_BETWEEN_REQUESTS_S)
        r = session.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        final_url = r.url
        status = r.status_code
        content_type = (r.headers.get("Content-Type") or "").lower()
        raw = r.content or b""
    except requests.Timeout:
        fetch_status = "timeout"
        error = "timeout"
    except Exception as e:
        fetch_status = "unknown"
        error = str(e)

    bytes_downloaded = len(raw)

    # classify early failures
    if error:
        doc = FetchedDoc(
            url=url, final_url=final_url, status_code=status,
            fetch_status=fetch_status, content_type=content_type,
            bytes_downloaded=bytes_downloaded, error=error
        )
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
        return doc

    if status >= 400:
        doc = FetchedDoc(
            url=url, final_url=final_url, status_code=status,
            fetch_status="http_error", content_type=content_type,
            bytes_downloaded=bytes_downloaded, error=f"HTTP {status}"
        )
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
        return doc

    # PDF?
    is_pdf = ("application/pdf" in content_type) or final_url.lower().endswith(".pdf")
    if is_pdf:
        if not HAS_PDFMINER:
            doc = FetchedDoc(
                url=url, final_url=final_url, status_code=status,
                fetch_status="pdf_no_parser", content_type=content_type,
                bytes_downloaded=bytes_downloaded,
                title="", author="", published_date="", site_name="",
                html="", text="", meta={}
            )
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
            return doc

        # Save PDF temp to extract text
        tmp_path = os.path.join(cache_dir, f"{cache_key}.pdf")
        with open(tmp_path, "wb") as f:
            f.write(raw)
        try:
            pdf_text = pdf_extract_text(tmp_path) or ""
        except Exception as e:
            pdf_text = ""
            error = f"pdf_parse_failed: {e}"

        doc = FetchedDoc(
            url=url, final_url=final_url, status_code=status,
            fetch_status="pdf", content_type=content_type,
            bytes_downloaded=bytes_downloaded,
            title="", author="", published_date="", site_name="",
            html="", text=pdf_text, meta={}, error=error
        )
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
        return doc

    # HTML/text
    html = raw.decode(errors="replace")
    html = sanitize_html(html)

    soup = BeautifulSoup(html, "lxml")
    meta = extract_meta(soup)

    title = meta.get("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    site_name = meta.get("og:site_name") or meta.get("application-name") or ""
    author = meta.get("author") or meta.get("article:author") or ""
    published = (
        meta.get("article:published_time")
        or meta.get("pubdate")
        or meta.get("date")
        or meta.get("dc.date")
        or ""
    )
    published_norm = normalize_date(published)

    # main text extraction
    text = extract_main_text(html, soup)

    # classify block/paywall/xml
    if looks_like_xml(content_type, html):
        fetch_status = "xml"
    elif status in (401, 403):
        fetch_status = "blocked"
    elif detect_paywall_or_block(title + "\n" + text):
        fetch_status = "paywall"
    else:
        fetch_status = "ok"

    doc = FetchedDoc(
        url=url, final_url=final_url, status_code=status,
        fetch_status=fetch_status, content_type=content_type,
        bytes_downloaded=bytes_downloaded,
        html=html, text=text,
        title=title, author=author,
        published_date=published_norm,
        site_name=site_name, meta=meta, error=""
    )

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
    return doc

def extract_main_text(html: str, soup: BeautifulSoup) -> str:
    if HAS_READABILITY and html:
        try:
            doc = Document(html)
            cleaned = doc.summary()
            soup2 = BeautifulSoup(cleaned, "lxml")
            return soup2.get_text("\n", strip=True)
        except Exception:
            pass
    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()
    return soup.get_text("\n", strip=True)

def crawl_site_pages(session: requests.Session, base_url: str, cache_dir: str) -> List[FetchedDoc]:
    pages: List[FetchedDoc] = []
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    for path in CRAWL_PATHS:
        candidate = urljoin(root, path)
        doc = fetch_doc(session, candidate, cache_dir)
        if doc.fetch_status in ("ok", "pdf", "xml") and len(doc.text or "") > 200:
            pages.append(doc)

    # Deduplicate by final_url
    dedup: Dict[str, FetchedDoc] = {}
    for p in pages:
        dedup[p.final_url] = p
    return list(dedup.values())


# -----------------------------
# Relationship + intended use
# -----------------------------

def infer_relation(domain: str, intended_use: str, relation_arg: str) -> str:
    # If user explicitly sets relation, respect it.
    if relation_arg and relation_arg != "auto":
        return relation_arg

    # Conservative defaults:
    # - For factual support (B), treat official sources as "self" unless user says otherwise.
    # - For narrative (A), self is fine.
    if domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn"):
        return "self" if intended_use in ("A", "B") else "unknown"

    return "unknown"


# -----------------------------
# Gating rules (v2)
# -----------------------------

def gate_source(
    main: FetchedDoc,
    aux_pages: List[FetchedDoc],
    domain_registry: Dict[str, Any],
    intended_use: str,
    relation: str
) -> Dict[str, Any]:
    """
    Gating logic:
    - Auto-REJECT only for clear cases: satire, known spam domain, unretrievable/404-style http_error, strong spam bundle.
    - Do NOT auto-reject merely for minimal text or missing outbound links (those become scoring penalties).
    - Auto-RESTRICT for official/state/party sources ONLY when relation == self (self-interest test).
    """
    domain = get_registered_domain(main.final_url or main.url)
    reg = domain_registry.get(domain, {})

    reasons: List[str] = []
    warnings_list: List[str] = []
    auto_reject = False
    auto_restrict = False
    restrict_reason = ""

    # Fetch failures: cannot score confidently; do not call it "spam".
    if main.fetch_status in ("timeout", "blocked", "paywall"):
        warnings_list.append(f"Fetch incomplete: {main.fetch_status} (manual retrieval needed for reliable scoring).")

    if main.fetch_status in ("http_error",) and main.status_code >= 400:
        reasons.append(f"Unretrievable (HTTP {main.status_code}).")
        auto_reject = True

    # Satire / parody
    if domain in KNOWN_SATIRE_DOMAINS or reg.get("satire_publisher", False):
        reasons.append("Satire/parody publisher.")
        auto_reject = True

    combined = " ".join([
        main.title or "",
        main.site_name or "",
        (main.text or "")[:2500],
        " ".join((p.text or "")[:1200] for p in aux_pages[:3])
    ]).lower()

    if (("satire" in combined or "parody" in combined) and any(k in combined for k in SATIRE_KEYWORDS)):
        reasons.append("Page indicates satire/parody/entertainment-only.")
        auto_reject = True

    if domain in KNOWN_BAD_DOMAINS or reg.get("known_bad", False):
        reasons.append("Known bad/misinfo/spam domain.")
        auto_reject = True

    # Spam/synthetic: require multiple signals (to avoid hurting legit minimal pages)
    spam_signals = 0
    if len((main.text or "")) < 200:
        spam_signals += 1
    if not main.title:
        spam_signals += 1
    if len(aux_pages) == 0:
        spam_signals += 1
    if re.search(r'\b(lorem ipsum|casino|crypto giveaway|porn|adult)\b', combined):
        spam_signals += 2

    if spam_signals >= 4 and not (domain.endswith(".gov") or domain.endswith(".gov.cn")):
        reasons.append("Likely low-credibility spam/synthetic content (multiple signals).")
        auto_reject = True

    # Auto-restrict official/state media only when SELF (self-interest test)
    is_official = domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn")
    is_state_controlled = reg.get("state_owned", False) or reg.get("party_owned", False) or reg.get("state_media", False) or is_official

    if is_state_controlled and relation == "self":
        auto_restrict = True
        restrict_reason = "State/party/official source in self-interest context: use as official narrative only (A), not independent factual proof."

    # Intended use: if user asked for B but source is auto-restricted, note it
    if auto_restrict and intended_use == "B":
        warnings_list.append("Intended use is factual support (B) but source is restricted to narrative use (A) due to self-interest.")

    return {
        "auto_reject": auto_reject,
        "reasons": reasons,
        "warnings": warnings_list,
        "auto_restrict": auto_restrict,
        "restrict_reason": restrict_reason
    }


# -----------------------------
# Heuristic scoring (C1–C10)
# -----------------------------

def score_0_2(v: int) -> int:
    return max(0, min(2, int(v)))

def score_criteria_heuristic(
    main: FetchedDoc,
    aux_pages: List[FetchedDoc],
    domain_registry: Dict[str, Any],
    all_main: List[FetchedDoc],
    intended_use: str,
    relation: str
) -> Dict[str, Criterion]:
    domain = get_registered_domain(main.final_url or main.url)
    reg = domain_registry.get(domain, {})
    combined_text = "\n".join([main.title, main.site_name, main.text] + [p.text for p in aux_pages if p.text])
    low = (combined_text or "").lower()

    crit: Dict[str, Criterion] = {}

    # C1 ownership/control
    c1 = 1
    reason = "Unknown ownership/control; defaulting to partial independence."
    if domain.endswith(".gov") or domain.endswith(".gov.cn") or reg.get("state_owned") or reg.get("party_owned") or reg.get("state_media"):
        c1 = 0
        reason = "Signals indicate state/party/official control."
    elif reg.get("independent", False) or "editorial independence" in low:
        c1 = 2
        reason = "Signals indicate editorial independence."
    crit["C1"] = Criterion(score_0_2(c1), reason, [])

    # C2 conflict-of-interest (claim-relative)
    # self = 0, adversary = 1, third_party/non_political_fact = 2 (default conservative if unknown)
    c2 = 1
    if relation == "self":
        c2 = 0
        reason = "Publisher has direct stake (self-interest context)."
    elif relation == "adversary":
        c2 = 1
        reason = "Publisher may have material incentives (adversarial context)."
    elif relation in ("third_party", "non_political_fact"):
        c2 = 2
        reason = "No direct stake indicated for this claim relationship."
    else:
        c2 = 1
        reason = "Claim relationship unknown; defaulting to some stake/unknown."
    crit["C2"] = Criterion(score_0_2(c2), reason, [])

    # C3 evidence strength
    if re.search(r'\b(court|judge|filing|indictment|statute|law|dataset|data)\b', low):
        crit["C3"] = Criterion(2, "Primary-evidence indicators present (legal docs/data).", [])
    elif re.search(r'\b(according to|reported by|source:)\b', low) or re.search(r'https?://', main.html or ""):
        crit["C3"] = Criterion(1, "Secondary reporting with references detected.", [])
    else:
        crit["C3"] = Criterion(0, "No clear evidence trail detected in fetched text.", [])

    # C4 method transparency
    if re.search(r'\b(method|methodology|how we reported|we interviewed|we reviewed)\b', low):
        crit["C4"] = Criterion(2, "Method/verification language present.", [])
    elif len(aux_pages) > 0:
        crit["C4"] = Criterion(1, "Some site-level policy pages found; method not explicit.", [])
    else:
        crit["C4"] = Criterion(0, "No method/verification described.", [])

    # C5 specificity/auditability
    date_hits = len(re.findall(r'\b(20\d{2}|19\d{2})\b', main.text or ""))
    number_hits = len(re.findall(r'\b\d{2,}\b', main.text or ""))
    if date_hits >= 3 and number_hits >= 6 and len((main.text or "")) > 800:
        crit["C5"] = Criterion(2, "High specificity (multiple dates/numbers; substantive text).", [])
    elif len((main.text or "")) < 250:
        crit["C5"] = Criterion(0, "Low specificity/minimal extracted text.", [])
    else:
        crit["C5"] = Criterion(1, "Moderate specificity.", [])

    # C6 corroboration within provided set only (weak heuristic)
    # For v2, keep conservative: requires overlap with other *different domains*
    main_terms = set(re.findall(r'\b[A-Z][a-z]{3,}\b', (main.text or "")[:5000]))
    matches = 0
    if len(main_terms) >= 5:
        for other in all_main:
            if other.final_url == main.final_url:
                continue
            other_domain = get_registered_domain(other.final_url or other.url)
            if other_domain == domain:
                continue
            other_terms = set(re.findall(r'\b[A-Z][a-z]{3,}\b', (other.text or "")[:5000]))
            if len(main_terms.intersection(other_terms)) >= 8:
                matches += 1
    if matches >= 2:
        crit["C6"] = Criterion(2, "Likely corroborated by multiple other sources in provided set.", [])
    elif matches == 1:
        crit["C6"] = Criterion(1, "Likely corroborated by at least one other source in provided set.", [])
    else:
        crit["C6"] = Criterion(0, "No corroboration detected within provided set.", [])

    # C7 legal/institutional confirmation
    if re.search(r'\b(confirmed|ruled|found|convicted)\b', low) and re.search(r'\b(court|judge)\b', low):
        crit["C7"] = Criterion(2, "Strong legal-confirmation language detected.", [])
    elif re.search(r'\b(court|judge|ruling|verdict|indictment|charges filed|case number)\b', low):
        crit["C7"] = Criterion(1, "Legal-process language detected; confirmation may be partial.", [])
    else:
        crit["C7"] = Criterion(0, "No legal/institutional confirmation signals detected.", [])

    # C8 corrections/track record signal (site-level)
    has_corrections = any(("correction" in (p.final_url or "").lower()) or ("correction" in (p.text or "").lower()) for p in aux_pages)
    if reg.get("tertiary_reference", False):
        crit["C8"] = Criterion(1, "Reference/tertiary source; track record depends on downstream sources.", [])
    elif has_corrections or "corrections policy" in low or "we correct" in low:
        crit["C8"] = Criterion(2, "Corrections/retractions behavior indicated.", [])
    else:
        crit["C8"] = Criterion(1, "Track record unknown/mixed.", [])

    # C9 bias handling/nuance
    hedge = len(re.findall(r'\b(alleged|reportedly|may|might|unclear|according to)\b', (main.text or "").lower()))
    absolutes = len(re.findall(r'\b(always|never|everyone|no one|obviously|undeniable)\b', (main.text or "").lower()))
    if hedge >= 5 and absolutes <= 1:
        crit["C9"] = Criterion(2, "Hedging/attribution suggests nuance.", [])
    elif absolutes >= 4 and hedge == 0:
        crit["C9"] = Criterion(0, "Absolutist framing with little uncertainty.", [])
    else:
        crit["C9"] = Criterion(1, "Neutral/unknown nuance level.", [])

    # C10 domain competence / threat models
    if re.search(r'\b(algorithm|surveillance|metadata|sanctions|shell company|beneficial owner|forensic|blockchain)\b', low) and re.search(r'\b(method|dataset|analysis|we reviewed)\b', low):
        crit["C10"] = Criterion(2, "Domain-competence signals present (technical/financial + method).", [])
    elif re.search(r'\b(ai|blockchain|kleptocracy|surveillance)\b', low) and not re.search(r'\b(method|dataset|analysis)\b', low):
        crit["C10"] = Criterion(0, "Buzzword-heavy with limited method/evidence signals.", [])
    else:
        crit["C10"] = Criterion(1, "Generalist coverage / unclear domain depth.", [])

    # Apply intended-use caps: if intended_use == A, don't punish evidence too hard (still score, but recommendation will differ)
    # We keep scoring as-is; recommendation logic handles A/B/C.

    return crit


# -----------------------------
# LLM scoring (quote-validated)
# -----------------------------

LLM_JSON_KEYS = ["C1","C2","C3","C4","C5","C6","C7","C8","C9","C10"]

def build_evidence_pack(main: FetchedDoc, aux_pages: List[FetchedDoc]) -> str:
    parts = []
    parts.append("=== SOURCE METADATA ===")
    parts.append(f"URL: {main.final_url}")
    parts.append(f"Domain: {get_registered_domain(main.final_url)}")
    parts.append(f"Title: {main.title}")
    parts.append(f"Author: {main.author}")
    parts.append(f"Published: {main.published_date}")
    parts.append(f"Site name: {main.site_name}")
    parts.append(f"Fetch status: {main.fetch_status}")
    parts.append(f"Content type: {main.content_type}")
    parts.append("\n=== MAIN TEXT (clipped) ===")
    parts.append(clip(main.text or "", 9000))

    parts.append("\n=== SITE/ORG PAGES (clipped) ===")
    for p in aux_pages[:6]:
        parts.append(f"\n--- {p.final_url} ---")
        parts.append(clip(p.text or "", 2500))

    return "\n".join(parts)

def llm_score(
    evidence_pack: str,
    intended_use: str,
    relation: str,
    model: str,
    max_retries: int = 2
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Calls the LLM and expects JSON:
    {
      "criteria": {
        "C1": {"score":0..2,"reason":"...","evidence_quotes":["..."]},
        ...
      },
      "notes": "... optional",
      "total_0_20": int,
      "hsus_0_100": int
    }

    Validation:
    - scores are 0..2
    - evidence_quotes must appear verbatim in evidence_pack (or empty w/ 'insufficient evidence')
    - totals match

    Returns: (payload_dict, "") on success; (None, error_string) on failure.
    """
    if not HAS_OPENAI:
        return None, "OpenAI SDK not installed."
    if not os.getenv("OPENAI_API_KEY"):
        return None, "OPENAI_API_KEY not set."

    client = OpenAI()

    system = (
        "You are a strict source-evaluation judge.\n"
        "RULES:\n"
        "1) You MUST use ONLY the provided evidence pack. No outside knowledge.\n"
        "2) Return VALID JSON only. No markdown.\n"
        "3) For each criterion C1..C10: score 0/1/2, give a short reason, and provide 1–2 verbatim evidence quotes.\n"
        "4) If evidence is missing, write 'insufficient evidence' in the reason and use an empty evidence_quotes list.\n"
        "5) Compute total_0_20 as sum of scores, and hsus_0_100 as total_0_20*5.\n"
        "6) Do not mention any organization names in your reasoning.\n"
    )

    rubric = (
        f"Context:\n"
        f"- intended_use: {intended_use} (A=official narrative, B=factual support, C=analytic context)\n"
        f"- relation: {relation} (self=direct stake; third_party=limited stake; adversary=material incentives; non_political_fact=primary record; unknown=unknown)\n"
        "Scoring rubric (0–2):\n"
        "C1 Ownership/control\n"
        "C2 Conflict-of-interest vs claim (relation-aware)\n"
        "C3 Evidence strength\n"
        "C4 Method transparency\n"
        "C5 Specificity/auditability\n"
        "C6 Corroboration potential (only from evidence pack)\n"
        "C7 Legal/institutional confirmation\n"
        "C8 Track record/corrections signals\n"
        "C9 Bias handling/nuance\n"
        "C10 Domain competence\n"
        "\n"
        "Output JSON shape (strict):\n"
        "{\n"
        '  "criteria": {\n'
        '    "C1": {"score": 0|1|2, "reason": "...", "evidence_quotes": ["...", "..."]},\n'
        '    ...\n'
        '    "C10": {"score": 0|1|2, "reason": "...", "evidence_quotes": ["...", "..."]}\n'
        "  },\n"
        '  "notes": "optional",\n'
        '  "total_0_20": 0-20,\n'
        '  "hsus_0_100": 0-100\n'
        "}\n"
    )

    user = rubric + "\nEVIDENCE PACK:\n" + evidence_pack

    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                text={"format": {"type": "json_object"}},
            )

            out_text = (resp.output_text or "").strip()
            if not out_text:
                raise ValueError("LLM returned empty output_text")

            try:
                data = json.loads(out_text)
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON from model. First 300 chars: {out_text[:300]}")

            validate_llm_payload(data, evidence_pack)
            return data, ""

        except Exception as e:
            last_err = str(e)
            time.sleep(1.5 * (attempt + 1))

    return None, last_err


def validate_llm_payload(payload: Dict[str, Any], evidence_pack: str) -> None:
    if "criteria" not in payload or not isinstance(payload["criteria"], dict):
        raise ValueError("LLM payload missing 'criteria' dict.")

    crit = payload["criteria"]
    for k in LLM_JSON_KEYS:
        if k not in crit:
            raise ValueError(f"LLM payload missing {k}.")
        item = crit[k]
        if not isinstance(item, dict):
            raise ValueError(f"{k} is not an object.")
        sc = item.get("score")
        if sc not in (0, 1, 2):
            raise ValueError(f"{k}.score must be 0/1/2.")

        reason = str(item.get("reason", "")).strip()
        if not reason:
            raise ValueError(f"{k}.reason is missing/empty.")

        quotes = item.get("evidence_quotes", [])
        if quotes and not isinstance(quotes, list):
            raise ValueError(f"{k}.evidence_quotes must be a list.")

        # If no quotes, require "insufficient evidence" in the reason to prevent hand-waving.
        if (not quotes) and ("insufficient evidence" not in reason.lower()):
            raise ValueError(f"{k}: missing evidence_quotes without 'insufficient evidence' in reason.")

        for q in (quotes or [])[:2]:
            if not isinstance(q, str) or len(q.strip()) < 6:
                raise ValueError(f"{k}.evidence quote too short/invalid.")
            if q not in evidence_pack:
                raise ValueError(f"{k}.evidence quote not found in evidence pack (must be verbatim).")

    total = payload.get("total_0_20")
    hsus = payload.get("hsus_0_100")
    calc_total = sum(int(crit[k]["score"]) for k in LLM_JSON_KEYS)

    if total != calc_total:
        raise ValueError(f"total_0_20 mismatch: got {total}, expected {calc_total}.")
    if hsus != calc_total * 5:
        raise ValueError(f"hsus_0_100 mismatch: got {hsus}, expected {calc_total * 5}.")

# -----------------------------
# Recommendation mapping
# -----------------------------

def recommendation_from_hsus(hsus: int) -> str:
    if hsus >= 85:
        return "Preferred: primary factual support"
    if hsus >= 65:
        return "Usable with safeguards (corroborate for factual support)"
    if hsus >= 45:
        return "Context-only (not for key factual claims)"
    return "Do not use (except possibly as narrative if relevant)"

def apply_intended_use_policy(rec: str, intended_use: str) -> str:
    # If user asked for A: allow narrative use even at low HSUS (but still warn on quality).
    if intended_use == "A":
        if rec.startswith("Do not use"):
            return "Narrative-only: use to quote what was said (A), not as factual proof"
        return rec
    return rec


# -----------------------------
# Pipeline
# -----------------------------

def parse_works_cited_lines(path: str) -> List[Tuple[str, str]]:
    """
    Returns list of (group_label, raw_line).
    If a line is tab-separated like: "HRF<TAB>citation...", group_label = "HRF"
    else group_label = ""
    """
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f.read().splitlines():
            if not line.strip():
                continue
            if "\t" in line:
                left, right = line.split("\t", 1)
                items.append((left.strip(), right.strip()))
            else:
                items.append(("", line.strip()))
    return items

def evaluate(
    items: List[Tuple[str, str]],
    intended_use: str,
    relation_arg: str,
    mode: str,
    llm_model: str,
    cache_dir: str = ".cache_sources_v2"
) -> List[SourceResult]:
    session = requests_session()
    registry = load_domain_registry(DOMAIN_REGISTRY_PATH)
    accessed = now_utc_date()

    # Extract URLs with labels preserved
    labeled_urls: List[Tuple[str, str]] = []
    for group, raw in items:
        urls = extract_urls_from_text(raw)
        for u in urls:
            labeled_urls.append((group, u))

    # Fetch all main docs first
    main_docs: List[Tuple[str, FetchedDoc]] = []
    for group, url in labeled_urls:
        doc = fetch_doc(session, url, cache_dir)
        main_docs.append((group, doc))

    results: List[SourceResult] = []

    for group, main in main_docs:
        domain = get_registered_domain(main.final_url or main.url)
        relation = infer_relation(domain, intended_use, relation_arg)

        aux_pages = crawl_site_pages(session, main.final_url or main.url, cache_dir)

        gating = gate_source(main, aux_pages, registry, intended_use, relation)

        # If auto-reject: score = 0 and stop
        if gating["auto_reject"]:
            works = format_works_cited(main, accessed)
            results.append(SourceResult(
                url=main.url,
                final_url=main.final_url,
                domain=domain,
                group_label=group,
                intended_use=intended_use,
                relation=relation,
                fetch_status=main.fetch_status,
                content_type=main.content_type,
                bytes_downloaded=main.bytes_downloaded,
                gating=gating,
                criteria={},
                total_0_20=0,
                hsus_0_100=0,
                recommendation="Do not use (auto-reject)",
                works_cited_entry=works,
                evidence_pages=sorted(set([main.final_url] + [p.final_url for p in aux_pages])),
                llm_used=False
            ))
            continue

        # If fetch failed or paywalled/blocked, we still produce a result but flag it.
        # We score heuristically from whatever text we have; confidence is conveyed via warnings.
        llm_used = False
        llm_error = ""

        # Heuristic baseline (always computed; may be overridden by LLM)
        heuristic = score_criteria_heuristic(main, aux_pages, registry, [d for _, d in main_docs], intended_use, relation)

        criteria: Dict[str, Criterion] = dict(heuristic)

        if mode in ("llm", "hybrid"):
            evidence_pack = build_evidence_pack(main, aux_pages)

            # Don't ask LLM to score if there is basically no evidence
            if len(evidence_pack) < 800 or main.fetch_status in ("timeout", "blocked", "paywall", "pdf_no_parser"):
                gating["warnings"].append("LLM skipped due to insufficient fetched evidence (or blocked/paywalled).")
            else:
                payload, err = llm_score(
                    evidence_pack=evidence_pack,
                    intended_use=intended_use,
                    relation=relation,
                    model=llm_model
                )
                if payload:
                    llm_used = True
                    llm_crit = payload["criteria"]

                    # Hybrid means keep deterministic C1/C2 from heuristics + registry/relationship
                    if mode == "hybrid":
                        for k in LLM_JSON_KEYS:
                            if k in ("C1", "C2"):
                                continue
                            item = llm_crit[k]
                            criteria[k] = Criterion(
                                score=int(item["score"]),
                                reason=str(item.get("reason","")).strip(),
                                evidence_quotes=item.get("evidence_quotes", [])[:2]
                            )
                    else:
                        for k in LLM_JSON_KEYS:
                            item = llm_crit[k]
                            criteria[k] = Criterion(
                                score=int(item["score"]),
                                reason=str(item.get("reason","")).strip(),
                                evidence_quotes=item.get("evidence_quotes", [])[:2]
                            )
                else:
                    llm_error = err
                    gating["warnings"].append(f"LLM failed/invalid; fell back to heuristics. ({err})")

        total = sum(c.score for c in criteria.values()) if criteria else 0
        total = max(0, min(20, total))
        hsus = total * 5

        rec = recommendation_from_hsus(hsus)

        # Apply auto-restrict policy
        if gating.get("auto_restrict"):
            rec = "Restricted: narrative/official position only"

        # Apply intended-use policy adjustment (esp. A)
        rec = apply_intended_use_policy(rec, intended_use)

        works = format_works_cited(main, accessed)
        evidence_pages = sorted(set([main.final_url] + [p.final_url for p in aux_pages]))

        results.append(SourceResult(
            url=main.url,
            final_url=main.final_url,
            domain=domain,
            group_label=group,
            intended_use=intended_use,
            relation=relation,
            fetch_status=main.fetch_status,
            content_type=main.content_type,
            bytes_downloaded=main.bytes_downloaded,
            gating=gating,
            criteria=criteria,
            total_0_20=total,
            hsus_0_100=hsus,
            recommendation=rec,
            works_cited_entry=works,
            evidence_pages=evidence_pages,
            llm_used=llm_used,
            llm_error=llm_error
        ))

    return results


# -----------------------------
# Output
# -----------------------------

def to_json(results: List[SourceResult]) -> List[Dict[str, Any]]:
    out = []
    for r in results:
        out.append({
            "url": r.url,
            "final_url": r.final_url,
            "domain": r.domain,
            "group_label": r.group_label,
            "intended_use": r.intended_use,
            "relation": r.relation,
            "fetch_status": r.fetch_status,
            "content_type": r.content_type,
            "bytes_downloaded": r.bytes_downloaded,
            "gating": r.gating,
            "criteria": {
                k: {
                    "score": v.score,
                    "reason": v.reason,
                    "evidence_quotes": v.evidence_quotes
                } for k, v in r.criteria.items()
            },
            "total_0_20": r.total_0_20,
            "hsus_0_100": r.hsus_0_100,
            "recommendation": r.recommendation,
            "works_cited_entry": r.works_cited_entry,
            "evidence_pages": r.evidence_pages,
            "llm_used": r.llm_used,
            "llm_error": r.llm_error,
        })
    return out

def write_markdown(results: List[SourceResult], path: str) -> None:
    lines: List[str] = []
    lines.append("# Source Evaluation Report\n")
    lines.append(f"_Generated: {now_utc_date()}_\n")

    for r in results:
        title = r.criteria.get("C5").reason if r.criteria.get("C5") else ""
        lines.append(f"## {r.final_url}\n")
        if r.group_label:
            lines.append(f"- **Group:** {r.group_label}\n")
        lines.append(f"- **Domain:** {r.domain}\n")
        lines.append(f"- **Intended use:** {r.intended_use}\n")
        lines.append(f"- **Relation:** {r.relation}\n")
        lines.append(f"- **Fetch status:** {r.fetch_status} ({r.content_type or 'unknown'})\n")
        lines.append(f"- **HSUS (0–100):** {r.hsus_0_100}\n")
        lines.append(f"- **Total (0–20):** {r.total_0_20}\n")
        lines.append(f"- **Recommendation:** {r.recommendation}\n")
        lines.append(f"- **LLM used:** {r.llm_used}\n")
        if r.llm_error:
            lines.append(f"- **LLM error:** {r.llm_error}\n")

        if r.gating.get("reasons"):
            lines.append(f"- **Gate reasons:** {', '.join(r.gating['reasons'])}\n")
        if r.gating.get("warnings"):
            lines.append(f"- **Warnings:** {', '.join(r.gating['warnings'])}\n")
        if r.gating.get("auto_restrict"):
            lines.append(f"- **Restriction:** {r.gating.get('restrict_reason','Restricted')}\n")

        if r.gating.get("auto_reject"):
            lines.append("\n---\n")
            continue

        lines.append("\n### Criteria breakdown (0–2 each)\n")
        for k in sorted(r.criteria.keys()):
            c = r.criteria[k]
            lines.append(f"- **{k}: {c.score}** — {c.reason}")
            if c.evidence_quotes:
                for q in c.evidence_quotes[:2]:
                    q2 = re.sub(r"\s+", " ", q)[:240]
                    lines.append(f"  - Evidence quote: “{q2}”")

        lines.append("\n### Evidence pages fetched\n")
        for u in r.evidence_pages[:10]:
            lines.append(f"- {u}")
        if len(r.evidence_pages) > 10:
            lines.append(f"- (+{len(r.evidence_pages)-10} more)\n")

        lines.append("\n---\n")

    lines.append("\n# Works Cited\n")
    for r in results:
        lines.append(f"- {r.works_cited_entry}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# -----------------------------
# CLI
# -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--works-cited", default="", help="Path to Works Cited text file (any format).")
    ap.add_argument("--urls", default="", help="Comma-separated URLs (optional).")
    ap.add_argument("--out-md", default="report_v2.md", help="Markdown report path.")
    ap.add_argument("--out-json", default="report_v2.json", help="JSON output path.")
    ap.add_argument("--intended-use", choices=["A","B","C"], default="B", help="A=official narrative, B=factual support, C=analytic context.")
    ap.add_argument("--relation", choices=["auto","self","adversary","third_party","non_political_fact","unknown"], default="auto",
                    help="Claim relationship for COI logic (self-interest test). Use 'auto' for conservative default.")
    ap.add_argument("--mode", choices=["heuristic","llm","hybrid"], default="hybrid",
                    help="heuristic: no LLM; llm: LLM for all; hybrid: LLM for C3+ while keeping C1/C2 deterministic.")
    ap.add_argument("--llm-model", default="gpt-5.2", help="LLM model name (must be available to your account).")
    args = ap.parse_args()

    items: List[Tuple[str, str]] = []
    if args.works_cited:
        items = parse_works_cited_lines(args.works_cited)

    urls: List[str] = []
    if args.urls:
        urls = [normalize_url(u) for u in args.urls.split(",") if u.strip()]

    if not items and urls:
        # treat urls list as unlabeled items
        items = [("", u) for u in urls]

    if not items:
        raise SystemExit("No input found. Provide --works-cited or --urls.")

    results = evaluate(
        items=items,
        intended_use=args.intended_use,
        relation_arg=args.relation,
        mode=args.mode,
        llm_model=args.llm_model
    )

    write_markdown(results, args.out_md)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(to_json(results), f, ensure_ascii=False, indent=2)

    print(f"Wrote: {args.out_md}")
    print(f"Wrote: {args.out_json}")

if __name__ == "__main__":
    main()

