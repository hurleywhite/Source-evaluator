#!/usr/bin/env python3
"""
Source Evaluator v4 (HSUS 0–100) — BUILD 2026-01-26a

Holistic design (source-agnostic):
- Evidence-driven: score ONLY from fetched content (no outside knowledge).
- Completeness != credibility:
  - "complete/partial/failed" describes retrieval/extraction quality
  - HSUS describes quality of the evidence we actually saw
  - For intended use B, completeness caps recommendations (prevents slip-through without false condemnation).
- Intended use:
  A = official narrative (“what they claim”)
  B = factual support (“what happened”)
  C = analytic context (“background/interpretation”)
- Relationship ("relation") is about stake in the claim:
  self | adversary | third_party | non_political_fact | unknown
- Gating:
  - Auto-REJECT only for clear junk (satire, obvious spam, known bad, true-unretrievable 404/410/451).
  - Thin extraction is NOT auto-reject. It becomes "partial completeness / manual retrieval needed".
- Rubric C1..C10 (0–2), but some criteria can be N/A when not assessable from evidence.
  - N/A criteria are excluded from denominator.
  - HSUS is normalized to 0–100 using the denominator used.

LLM:
- Optional judge that only sees the evidence pack; must quote evidence.
- Hard timeouts + retries on the OpenAI client (no indefinite hangs).
- Validation:
  - Quotes must match evidence pack (robust whitespace/unicode normalization).
  - Scores must be 0/1/2 or null (for N/A).
  - Totals must match computed totals, based on assessed criteria only.

Outputs:
- Markdown report + JSON.
- Checkpoint JSON updated after each source (so long runs produce progress).
"""

import argparse
import dataclasses
import hashlib
import json
import logging
import multiprocessing as mp
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Literal
from urllib.parse import urlparse, urljoin

import requests
import tldextract
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dateutil import parser as dateparser

# Optional readability
try:
    from readability import Document
    HAS_READABILITY = True
except Exception:
    HAS_READABILITY = False

# Optional pdfminer
try:
    from pdfminer.high_level import extract_text as _pdf_extract_text
    HAS_PDFMINER = True
except Exception:
    HAS_PDFMINER = False

# Optional OpenAI
try:
    from openai import OpenAI
    HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# -----------------------------
# Config
# -----------------------------

BUILD_ID = "2026-01-26a"

DOMAIN_REGISTRY_PATH = "domain_registry.json"

USER_AGENT = "SourceEvaluatorBot/4.0 (+contact: research@yourcompany.example)"

# Request timeouts: (connect, read)
HTTP_TIMEOUT = (8, 20)
DEFAULT_SLEEP_S = 0.8

# LLM hard timeout (seconds)
LLM_TIMEOUT_S = 60.0
LLM_MAX_RETRIES = 2

# PDF parse timeout
PDF_PARSE_TIMEOUT_S = 25

# Max aux pages per domain
DEFAULT_MAX_AUX_PAGES = 6

CRAWL_PATHS = [
    "/about", "/about-us", "/who-we-are", "/mission", "/history", "/our-story",
    "/contact", "/contact-us",
    "/editorial-policy", "/ethics", "/standards", "/values", "/principles",
    "/methods", "/methodology",
    "/corrections", "/correction", "/retractions",
    "/terms", "/privacy", "/policies"
]

SATIRE_KEYWORDS = ["satire", "parody", "humor", "humour", "comedy", "entertainment"]
KNOWN_SATIRE_DOMAINS = {"theonion.com", "babylonbee.com", "clickhole.com"}

KNOWN_BAD_DOMAINS = set()  # optional

PAYWALL_HINTS = [
    "subscribe to continue", "subscribe now", "sign in to continue",
    "membership required", "register to continue", "start your subscription",
    "enable cookies", "enable javascript",
    "access denied", "unusual traffic", "verify you are human", "captcha",
]
BOTBLOCK_HINTS = [
    "verify you are human", "captcha", "cloudflare", "unusual traffic",
    "access denied", "request blocked", "bot detection", "ddos protection",
]

# “About/self” page heuristics (generic)
SELF_PAGE_HINTS = [
    "/about", "/about-us", "/who-we-are", "/mission", "/history", "/our-story",
    "/our-mission", "/what-we-do"
]

# Listing/section page heuristics (generic)
LISTING_URL_HINTS = [
    "/section/", "/category/", "/tag/", "/topics/", "/topic/", "/search", "/archive", "/archives"
]

# -----------------------------
# Data models
# -----------------------------

@dataclass
class FetchedDoc:
    url: str
    final_url: str
    status_code: int
    fetch_status: str   # ok|http_error|timeout|blocked|paywall|pdf|pdf_no_parser|xml|unknown
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
    score: Optional[int]          # 0/1/2 or None for N/A
    assessed: bool                # True if score is counted in denominator
    reason: str
    evidence_quotes: List[str] = field(default_factory=list)

@dataclass
class SourceResult:
    url: str
    final_url: str
    domain: str
    group_label: str
    intended_use: str
    relation: str

    fetch_status: str
    content_type: str
    bytes_downloaded: int

    completeness: str             # complete|partial|failed
    page_type: str                # article|listing|unknown
    confidence: str               # high|medium|low (derived)
    gating: Dict[str, Any]

    criteria: Dict[str, Criterion]
    points_scored: int            # sum of assessed criterion scores (0..2 per assessed)
    denom_points: int             # 2 * assessed_criteria_count
    hsus_0_100: int               # normalized: round(points_scored/denom_points*100)
    recommendation: str

    works_cited_entry: str
    evidence_pages: List[str]

    llm_used: bool
    llm_error: str = ""

# -----------------------------
# Utility helpers
# -----------------------------

def now_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def normalize_url(url: str) -> str:
    url = (url or "").strip().strip(").,;\"'")
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
    text = text or ""
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
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

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
    return (s or "").strip()[:n]

def sanitize_html(html: str) -> str:
    if not html:
        return ""
    html = html.replace("\x00", "")
    html = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", "", html)
    return html

def extract_meta(soup: BeautifulSoup) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for tag in soup.find_all("meta"):
        if tag.get("property") and tag.get("content"):
            meta[tag["property"].strip().lower()] = tag["content"].strip()
        if tag.get("name") and tag.get("content"):
            meta[tag["name"].strip().lower()] = tag["content"].strip()
    return meta

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

def detect_paywall_or_block(text: str) -> Tuple[bool, bool]:
    t = (text or "").lower()
    paywall = any(h in t for h in PAYWALL_HINTS)
    botblock = any(h in t for h in BOTBLOCK_HINTS)
    return paywall, botblock

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

def norm_for_quote_match(s: str) -> str:
    s = (s or "")
    s = s.replace("\u00A0", " ")
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def extract_snippets(text: str, pattern: str, max_snips: int = 2, window: int = 140) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    for m in re.finditer(pattern, text, flags=re.IGNORECASE):
        start = max(0, m.start() - window)
        end = min(len(text), m.end() + window)
        snip = re.sub(r"\s+", " ", text[start:end]).strip()
        if snip and snip not in out:
            out.append(snip)
        if len(out) >= max_snips:
            break
    return out

# -----------------------------
# Extraction
# -----------------------------

def extract_main_text(html: str, soup: BeautifulSoup) -> str:
    if HAS_READABILITY and html:
        try:
            doc = Document(html)
            cleaned = doc.summary()
            soup2 = BeautifulSoup(cleaned, "lxml")
            txt = soup2.get_text("\n", strip=True)
            if len(txt) >= 200:
                return txt
        except Exception:
            pass

    for tag_name in ("article", "main"):
        tag = soup.find(tag_name)
        if tag:
            for bad in tag(["script", "style", "noscript"]):
                bad.decompose()
            txt = tag.get_text("\n", strip=True)
            if len(txt) >= 250:
                return txt

    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()
    return soup.get_text("\n", strip=True)

def classify_page_type(final_url: str, html: str, text: str) -> str:
    u = (final_url or "").lower()
    if any(h in u for h in LISTING_URL_HINTS):
        return "listing"
    # crude link-density heuristic
    if html:
        try:
            soup = BeautifulSoup(html, "lxml")
            links = len(soup.find_all("a"))
            body_len = len((text or "").strip())
            if links >= 80 and body_len < 1200:
                return "listing"
        except Exception:
            pass
    return "unknown"

def infer_relation_from_url(main: FetchedDoc, relation_arg: str) -> str:
    if relation_arg and relation_arg != "auto":
        return relation_arg

    domain = get_registered_domain(main.final_url or main.url)
    path = (urlparse(main.final_url or main.url).path or "").lower()

    # Official domains -> self for A/B by default
    if domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn"):
        return "self"

    # About/mission pages -> self (generic)
    if any(h in path for h in SELF_PAGE_HINTS):
        return "self"

    return "unknown"

# -----------------------------
# PDF extraction with timeout
# -----------------------------

def _pdf_worker(pdf_path: str, q: mp.Queue) -> None:
    try:
        from pdfminer.high_level import extract_text as pdf_extract_text
        q.put(pdf_extract_text(pdf_path) or "")
    except Exception:
        q.put("")

def extract_pdf_text_with_timeout(pdf_path: str, timeout_s: int = PDF_PARSE_TIMEOUT_S) -> Tuple[str, str]:
    if not HAS_PDFMINER:
        return "", "pdf_no_parser"
    q: mp.Queue = mp.Queue()
    p = mp.Process(target=_pdf_worker, args=(pdf_path, q))
    p.start()
    p.join(timeout_s)
    if p.is_alive():
        p.terminate()
        p.join()
        return "", "pdf_parse_timeout"
    try:
        text = q.get_nowait()
    except Exception:
        text = ""
    return text, ""

# -----------------------------
# Fetching
# -----------------------------

def fetch_doc(session: requests.Session, url: str, cache_dir: str, sleep_s: float) -> FetchedDoc:
    os.makedirs(cache_dir, exist_ok=True)
    cache_key = safe_filename(url)
    cache_path = os.path.join(cache_dir, f"{cache_key}.json")

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return FetchedDoc(**json.load(f))
        except Exception:
            pass

    final_url = url
    status = 0
    content_type = ""
    raw = b""
    fetch_status = "unknown"
    error = ""

    try:
        time.sleep(max(0.0, float(sleep_s)))
        r = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
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

    is_pdf = ("application/pdf" in content_type) or final_url.lower().endswith(".pdf")
    if is_pdf:
        if bytes_downloaded > 20_000_000:
            doc = FetchedDoc(
                url=url, final_url=final_url, status_code=status,
                fetch_status="pdf", content_type=content_type,
                bytes_downloaded=bytes_downloaded, text="",
                error="pdf_skipped_large_file"
            )
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
            return doc

        tmp_path = os.path.join(cache_dir, f"{cache_key}.pdf")
        with open(tmp_path, "wb") as f:
            f.write(raw)

        pdf_text, pdf_err = extract_pdf_text_with_timeout(tmp_path, timeout_s=PDF_PARSE_TIMEOUT_S)
        doc = FetchedDoc(
            url=url, final_url=final_url, status_code=status,
            fetch_status="pdf" if HAS_PDFMINER else "pdf_no_parser",
            content_type=content_type,
            bytes_downloaded=bytes_downloaded,
            text=pdf_text,
            error=pdf_err
        )
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
        return doc

    html = sanitize_html(raw.decode(errors="replace"))
    html_excerpt = clip(html, 300000)
    soup = BeautifulSoup(html_excerpt, "lxml")
    meta = extract_meta(soup)

    title = meta.get("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    site_name = meta.get("og:site_name") or meta.get("application-name") or ""
    author = meta.get("author") or meta.get("article:author") or ""
    published = (
        meta.get("article:published_time")
        or meta.get("pubdate")
        or meta.get("date")
        or meta.get("dc.date")
        or meta.get("datepublished")
        or ""
    )
    published_norm = normalize_date(published)

    text = extract_main_text(html_excerpt, soup)

    # Fallback extraction if too short (general, not domain-specific)
    if len((text or "").strip()) < 350:
        try:
            soup2 = BeautifulSoup(html_excerpt, "lxml")
            for bad in soup2(["script", "style", "noscript"]):
                bad.decompose()
            text2 = soup2.get_text("\n", strip=True)
            if len((text2 or "").strip()) > len((text or "").strip()):
                text = text2
        except Exception:
            pass

    if looks_like_xml(content_type, html_excerpt):
        fetch_status = "xml"
    else:
        paywall, botblock = detect_paywall_or_block((title or "") + "\n" + (text or ""))
        if status in (401, 403) or botblock:
            fetch_status = "blocked"
        elif paywall:
            fetch_status = "paywall"
        else:
            fetch_status = "ok"

    doc = FetchedDoc(
        url=url, final_url=final_url, status_code=status,
        fetch_status=fetch_status, content_type=content_type,
        bytes_downloaded=bytes_downloaded,
        html=html_excerpt, text=text,
        title=title, author=author,
        published_date=published_norm,
        site_name=site_name,
        meta=meta,
        error=""
    )
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
    return doc

# -----------------------------
# Aux crawling
# -----------------------------

def discover_policy_links_from_html(base_url: str, html: str, max_links: int = 12) -> List[str]:
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")

    ALLOW_TOKENS = (
        "about", "about-us", "who-we-are", "mission", "our-mission", "history",
        "standards", "editorial", "ethics", "guidelines", "values", "principles",
        "corrections", "clarifications", "retractions", "accuracy",
        "code-of-ethics", "code-of-conduct",
        "contact", "privacy", "terms", "policy", "policies",
        "transparency", "governance", "ownership",
    )

    BLOCK_PATTERNS = re.compile(r"/(article|news|story|stories|video|videos|gallery|photo|photos|live|interactive)/", re.I)

    links: List[str] = []

    # Prefer footer/nav links
    candidates = []
    for container in soup.find_all(["footer", "nav"]):
        candidates.extend(container.find_all("a", href=True))
    if not candidates:
        candidates = soup.find_all("a", href=True)

    for a in candidates:
        href = (a.get("href") or "").strip()
        if not href:
            continue
        absu = urljoin(base_url, href)
        hlow = absu.lower()

        if BLOCK_PATTERNS.search(hlow):
            continue

        txt = (a.get_text(" ", strip=True) or "").lower()

        if any(t in txt for t in ALLOW_TOKENS) or any(t in hlow for t in ALLOW_TOKENS):
            links.append(absu)

    root = get_registered_domain(base_url)
    out: List[str] = []
    seen = set()
    for u in links:
        if get_registered_domain(u) != root:
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_links:
            break

    return out

def crawl_site_pages(
    session: requests.Session,
    main: FetchedDoc,
    cache_dir: str,
    sleep_s: float,
    max_aux_pages: int = 6,
) -> List[FetchedDoc]:
    pages: List[FetchedDoc] = []
    if not main.final_url:
        return pages

    parsed = urlparse(main.final_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    # 1) Probe standard policy paths
    for path in CRAWL_PATHS:
        doc = fetch_doc(session, urljoin(root, path), cache_dir, sleep_s)
        if doc.fetch_status in ("ok", "pdf", "xml") and len((doc.text or "")) > 200:
            pages.append(doc)
        if len(pages) >= max_aux_pages:
            break

    # 2) Discover policy links from footer/nav of the main page
    if len(pages) < max_aux_pages:
        discovered = discover_policy_links_from_html(main.final_url, main.html or "", max_links=14)
        for u in discovered:
            doc = fetch_doc(session, u, cache_dir, sleep_s)
            if doc.fetch_status in ("ok", "pdf", "xml") and len((doc.text or "")) > 200:
                pages.append(doc)
            if len(pages) >= max_aux_pages:
                break

    # Deduplicate by final_url
    dedup: Dict[str, FetchedDoc] = {}
    for p in pages:
        dedup[p.final_url] = p

    return list(dedup.values())


# -----------------------------
# Gating + completeness
# -----------------------------

def compute_completeness(main: FetchedDoc, page_type: str) -> str:
    # failed: cannot fetch meaningful content
    if main.fetch_status in ("timeout", "blocked", "paywall", "pdf_no_parser"):
        return "failed"
    if main.fetch_status == "http_error":
        return "failed"
    # partial: ok fetch but thin extraction OR listing page
    if page_type == "listing":
        return "partial"
    if main.fetch_status == "ok" and len((main.text or "").strip()) < 500:
        return "partial"
    # complete: ok and sufficient body
    if main.fetch_status in ("ok", "pdf") and len((main.text or "").strip()) >= 500:
        return "complete"
    return "partial"

def gate_source(main: FetchedDoc, aux_pages: List[FetchedDoc], registry: Dict[str, Any], page_type: str) -> Dict[str, Any]:
    domain = get_registered_domain(main.final_url or main.url)
    reg = registry.get(domain, {})
    reasons: List[str] = []
    warnings_list: List[str] = []
    auto_reject = False

    combined = " ".join([
        main.title or "",
        main.site_name or "",
        (main.text or "")[:3000],
        " ".join((p.text or "")[:1200] for p in aux_pages[:2]),
    ]).lower()

    # True-unretrievable only (clear cases)
    if main.fetch_status == "http_error" and main.status_code in (404, 410, 451):
        reasons.append(f"Unretrievable (HTTP {main.status_code}).")
        auto_reject = True

    # Satire / parody
    if domain in KNOWN_SATIRE_DOMAINS or reg.get("satire_publisher", False):
        reasons.append("Satire/parody publisher.")
        auto_reject = True
    if (("satire" in combined or "parody" in combined) and any(k in combined for k in SATIRE_KEYWORDS)):
        reasons.append("Page indicates satire/parody/entertainment-only.")
        auto_reject = True

    # Known bad domains
    if domain in KNOWN_BAD_DOMAINS or reg.get("known_bad", False):
        reasons.append("Known bad/misinfo/spam domain.")
        auto_reject = True

    # Spam/synthetic: multi-signal (do NOT use thin extraction alone)
    spam_signals = 0
    if len((main.text or "")) < 180:
        spam_signals += 1
    if not main.title:
        spam_signals += 1
    if len(aux_pages) == 0:
        spam_signals += 1
    if re.search(r'\b(lorem ipsum|casino|crypto giveaway|porn|adult)\b', combined):
        spam_signals += 2

    if spam_signals >= 4 and not (domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn")):
        reasons.append("Likely spam/synthetic content (multiple signals).")
        auto_reject = True

    # Non-fatal warnings
    if page_type == "listing":
        warnings_list.append("Final URL appears to be a listing/section page; may not be the cited article.")
    if main.fetch_status in ("timeout", "blocked", "paywall", "pdf_no_parser"):
        warnings_list.append(f"Fetch incomplete: {main.fetch_status}. Manual retrieval recommended.")
    if main.fetch_status == "ok" and len((main.text or "").strip()) < 500:
        warnings_list.append("Thin extracted text; page may be JS-rendered or extraction incomplete.")

    return {
        "auto_reject": auto_reject,
        "reasons": reasons,
        "warnings": warnings_list,
    }

def confidence_from_completeness(completeness: str, llm_used: bool) -> str:
    if completeness == "complete":
        return "high" if llm_used else "medium"
    if completeness == "partial":
        return "medium"
    return "low"

# -----------------------------
# Rubric scoring (heuristics baseline)
# -----------------------------

CRIT_KEYS = ["C1","C2","C3","C4","C5","C6","C7","C8","C9","C10"]

def criterion(score: Optional[int], assessed: bool, reason: str, quotes: Optional[List[str]] = None) -> Criterion:
    return Criterion(score=score, assessed=assessed, reason=reason, evidence_quotes=(quotes or [])[:2])

def score_heuristic(
    main: FetchedDoc,
    aux_pages: List[FetchedDoc],
    registry: Dict[str, Any],
    all_main: List[FetchedDoc],
    relation: str,
    page_type: str
) -> Dict[str, Criterion]:
    domain = get_registered_domain(main.final_url or main.url)
    reg = registry.get(domain, {})
    aux_text = "\n".join([p.text for p in aux_pages if p.text])
    combined_text = "\n".join([main.title or "", main.site_name or "", main.text or "", aux_text])
    low = combined_text.lower()

    crit: Dict[str, Criterion] = {}

    # C1 Ownership/control: N/A unless we have strong signals
    if reg.get("state_owned") or reg.get("party_owned") or reg.get("state_media") or domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn"):
        crit["C1"] = criterion(0, True, "Signals indicate state/party/official control.", [])
    elif reg.get("independent", False) or re.search(r'\beditorial independence\b', low):
        crit["C1"] = criterion(2, True, "Signals indicate editorial independence.", extract_snippets(aux_text or combined_text, r'editorial independence|independent', 2))
    else:
        crit["C1"] = criterion(None, False, "Not assessed (insufficient ownership/control evidence in fetched pages).", [])

    # C2 COI: relation-aware (assessed)
    if relation == "self":
        crit["C2"] = criterion(0, True, "Self-interest context (publisher has direct stake).", [])
    elif relation == "adversary":
        crit["C2"] = criterion(1, True, "Adversarial context; possible incentives.", [])
    elif relation in ("third_party", "non_political_fact"):
        crit["C2"] = criterion(2, True, "Third-party or primary record context; limited direct stake.", [])
    else:
        crit["C2"] = criterion(1, True, "Relation unknown; treat as potentially interested.", [])

    # C3 Evidence strength: avoid keyword spoofing
    if main.fetch_status == "pdf":
        crit["C3"] = criterion(2, True, "Primary evidence format (PDF) extracted.", extract_snippets(main.text, r'\b(exhibit|appendix|case number|docket|statute|judgment|transcript|dataset)\b', 2))
    elif re.search(r'\b(docket|case number|judgment|indictment|filing|transcript|dataset)\b', low):
        crit["C3"] = criterion(2, True, "Primary-evidence indicators present (filing/judgment/transcript/dataset).", extract_snippets(combined_text, r'\b(docket|case number|judgment|indictment|filing|transcript|dataset)\b', 2))
    elif re.search(r'\b(according to|reported|said|source:)\b', low) or re.search(r'https?://', main.html or ""):
        crit["C3"] = criterion(1, True, "Secondary reporting with attribution/references detected.", extract_snippets(combined_text, r'\b(according to|reported|said|source:)\b', 2))
    else:
        crit["C3"] = criterion(0, True, "No clear evidence trail detected in fetched text.", [])

    # C4 Method transparency: can be supported by aux pages
    if re.search(r'\b(method|methodology|how we reported|we interviewed|we reviewed|we analyzed|we analysed)\b', low):
        crit["C4"] = criterion(2, True, "Method/verification language present.", extract_snippets(combined_text, r'\b(method|methodology|how we reported|we interviewed|we reviewed|we analyzed|we analysed)\b', 2))
    elif re.search(r'\b(standards|ethics|corrections|retractions|editorial)\b', aux_text.lower()):
        crit["C4"] = criterion(1, True, "Standards/policy pages found; item-level method not explicit.", extract_snippets(aux_text, r'\b(standards|ethics|corrections|retractions|editorial)\b', 2))
    else:
        crit["C4"] = criterion(0, True, "No method/verification described in fetched pages.", [])

    # C5 Specificity/auditability: do not punish listing pages as “vague”
    if page_type == "listing":
        crit["C5"] = criterion(None, False, "Not assessed (listing/section page; may not be the cited article).", [])
    else:
        date_hits = len(re.findall(r'\b(20\d{2}|19\d{2})\b', main.text or ""))
        number_hits = len(re.findall(r'\b\d{2,}\b', main.text or ""))
        body_len = len((main.text or "").strip())
        if body_len < 250:
            crit["C5"] = criterion(0, True, "Low extracted text; poor auditability.", [])
        elif date_hits >= 2 and number_hits >= 4 and body_len > 800:
            crit["C5"] = criterion(2, True, "High specificity: dates/numbers/actors present.", extract_snippets(main.text, r'\b(20\d{2}|19\d{2})\b|\b\d{2,}\b', 2))
        else:
            crit["C5"] = criterion(1, True, "Some specifics present; audit trail may be incomplete.", extract_snippets(main.text, r'\b(20\d{2}|19\d{2})\b|\b\d{2,}\b', 2))

    # C6 Corroboration within set: N/A if only one source
    if len(all_main) < 2:
        crit["C6"] = criterion(None, False, "Not assessed (only one source in set).", [])
    else:
        # lightweight overlap
        def features(text: str) -> set:
            ents = set(re.findall(r'\b[A-Z][a-z]{3,}\b', text[:6000]))
            nums = set(re.findall(r'\b\d{2,}\b', text[:6000]))
            yrs = set(re.findall(r'\b(19\d{2}|20\d{2})\b', text[:6000]))
            return set(list(ents)[:80]) | set(list(nums)[:60]) | set(list(yrs)[:20])

        my = features(main.text or "")
        matches = 0
        for other in all_main:
            if other.final_url == main.final_url:
                continue
            od = get_registered_domain(other.final_url or other.url)
            if od == domain:
                continue
            if len(my) >= 20 and len(my.intersection(features(other.text or ""))) >= 18:
                matches += 1

        if matches >= 2:
            crit["C6"] = criterion(2, True, "Likely corroborated by multiple independent sources in set.", [])
        elif matches == 1:
            crit["C6"] = criterion(1, True, "Likely corroborated by at least one independent source in set.", [])
        else:
            crit["C6"] = criterion(0, True, "No corroboration detected within provided set.", [])

    # C7 Legal/institutional confirmation: require more than a word “court”
    if re.search(r'\b(court|judge|prosecutor)\b', low) and re.search(r'\b(ruled|sentenced|convicted|indicted|charged)\b', low):
        crit["C7"] = criterion(2, True, "Institutional action + outcome language present.", extract_snippets(combined_text, r'\b(court|judge|prosecutor)\b.*\b(ruled|sentenced|convicted|indicted|charged)\b|\b(ruled|sentenced|convicted|indicted|charged)\b.*\b(court|judge|prosecutor)\b', 2))
    elif re.search(r'\b(court|judge|prosecutor|ruling|verdict|indictment|charges filed|case number|filing)\b', low):
        crit["C7"] = criterion(1, True, "Institutional process referenced; confirmation may be partial.", extract_snippets(combined_text, r'\b(court|judge|prosecutor|ruling|verdict|indictment|charges filed|case number|filing)\b', 2))
    else:
        crit["C7"] = criterion(0, True, "No legal/institutional confirmation signals detected.", [])

    # C8 Track record/corrections: N/A unless policy pages show it or registry says
    has_corrections = any(
        ("correction" in (p.final_url or "").lower())
        or ("retraction" in (p.final_url or "").lower())
        or re.search(r'\b(corrections|retractions|we correct)\b', (p.text or "").lower())
        for p in aux_pages
    )
    if reg.get("frequent_misinfo", False) or reg.get("known_bad", False):
        crit["C8"] = criterion(0, True, "Registry signals frequent misinformation or lack of correction behavior.", [])
    elif has_corrections or re.search(r'\b(corrections policy|we correct|retractions)\b', low):
        crit["C8"] = criterion(2, True, "Corrections/retractions behavior indicated.", extract_snippets(aux_text or combined_text, r'\b(corrections|retractions|we correct)\b', 2))
    else:
        crit["C8"] = criterion(None, False, "Not assessed (insufficient corrections/track record evidence in fetched pages).", [])

    # C9 Bias handling/nuance
    hedge = len(re.findall(r'\b(alleged|reportedly|may|might|unclear|according to)\b', low))
    absolutes = len(re.findall(r'\b(always|never|everyone|no one|obviously|undeniable)\b', low))
    if hedge >= 5 and absolutes <= 1:
        crit["C9"] = criterion(2, True, "Attribution/hedging suggests nuance and uncertainty handling.", extract_snippets(combined_text, r'\b(alleged|reportedly|may|might|unclear|according to)\b', 2))
    elif absolutes >= 4 and hedge == 0:
        crit["C9"] = criterion(0, True, "Absolutist framing with little uncertainty.", extract_snippets(combined_text, r'\b(always|never|everyone|no one|obviously|undeniable)\b', 2))
    else:
        crit["C9"] = criterion(1, True, "Some nuance/uncertainty handling; may be incomplete.", extract_snippets(combined_text, r'\b(alleged|reportedly|according to)\b', 2))

    # C10 Domain competence
    if re.search(r'\b(forensic|dataset|method|analysis|we reviewed|we analyzed|we analysed)\b', low) and re.search(r'\b(surveillance|metadata|sanctions|beneficial owner|shell company|blockchain|forensic)\b', low):
        crit["C10"] = criterion(2, True, "Specialized domain signals + method present.", extract_snippets(combined_text, r'\b(surveillance|metadata|sanctions|beneficial owner|shell company|blockchain|forensic)\b', 2))
    elif re.search(r'\b(ai|blockchain|kleptocracy|surveillance)\b', low) and not re.search(r'\b(method|dataset|analysis|we reviewed|we analyzed|we analysed)\b', low):
        crit["C10"] = criterion(0, True, "Buzzword-heavy with limited method/evidence signals.", extract_snippets(combined_text, r'\b(ai|blockchain|kleptocracy|surveillance)\b', 2))
    else:
        crit["C10"] = criterion(1, True, "Generalist coverage / unclear domain depth.", [])

    # Ensure all keys exist
    for k in CRIT_KEYS:
        if k not in crit:
            crit[k] = criterion(None, False, "Not assessed.", [])
    return crit

def compute_points(criteria: Dict[str, Criterion]) -> Tuple[int, int, int]:
    points = 0
    denom = 0
    for k in CRIT_KEYS:
        c = criteria[k]
        if c.assessed and c.score in (0, 1, 2):
            points += int(c.score)
            denom += 2
    hsus = int(round((points / denom) * 100)) if denom > 0 else 0
    return points, denom, hsus

# -----------------------------
# LLM scoring
# -----------------------------

def build_evidence_pack(main: FetchedDoc, aux_pages: List[FetchedDoc], page_type: str, completeness: str, relation: str, intended_use: str) -> str:
    parts: List[str] = []
    parts.append("=== CONTEXT ===")
    parts.append(f"intended_use: {intended_use}")
    parts.append(f"relation: {relation}")
    parts.append(f"page_type: {page_type}")
    parts.append(f"completeness: {completeness}")

    parts.append("\n=== SOURCE METADATA ===")
    parts.append(f"URL: {main.final_url}")
    parts.append(f"Domain: {get_registered_domain(main.final_url)}")
    parts.append(f"Title: {main.title}")
    parts.append(f"Author: {main.author}")
    parts.append(f"Published: {main.published_date}")
    parts.append(f"Site name: {main.site_name}")
    parts.append(f"Fetch status: {main.fetch_status}")
    parts.append(f"Content type: {main.content_type}")

    parts.append("\n=== MAIN TEXT (clipped) ===")
    parts.append(clip(main.text or "", 14000))

    parts.append("\n=== AUX PAGES (clipped) ===")
    for p in aux_pages[:6]:
        parts.append(f"\n--- {p.final_url} ---")
        parts.append(clip(p.text or "", 4500))

    return "\n".join(parts)

def validate_llm_payload(payload: Dict[str, Any], evidence_pack: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError("LLM payload is not an object.")
    if "criteria" not in payload or not isinstance(payload["criteria"], dict):
        raise ValueError("LLM payload missing 'criteria' dict.")
    crit = payload["criteria"]

    ep = norm_for_quote_match(evidence_pack)

    for k in CRIT_KEYS:
        if k not in crit or not isinstance(crit[k], dict):
            raise ValueError(f"Missing/invalid {k}.")
        item = crit[k]

        sc = item.get("score", None)
        # allow null for N/A
        if sc is not None and sc not in (0, 1, 2):
            raise ValueError(f"{k}.score must be 0/1/2 or null.")

        reason = (item.get("reason") or "").strip()
        quotes = item.get("evidence_quotes", [])
        if quotes and not isinstance(quotes, list):
            raise ValueError(f"{k}.evidence_quotes must be a list.")

        # Quotes required for assessed criteria unless "insufficient evidence"
        allow_no_quotes = {"C1", "C2", "C8", "C6"}  # can be N/A or inferential
        if sc is None:
            # if N/A, no quotes required; reason must indicate not assessed/insufficient
            if "not assessed" not in reason.lower() and "insufficient" not in reason.lower():
                raise ValueError(f"{k}: score is null but reason must say not assessed/insufficient evidence.")
            continue

        if not quotes:
            if "insufficient" not in reason.lower():
                if k in allow_no_quotes:
                    # allow but must be explicit
                    raise ValueError(f"{k}: if no quotes, reason must say 'insufficient evidence'.")
                raise ValueError(f"{k}: missing quotes; reason must say 'insufficient evidence'.")
        else:
            # accept short quotes if they match; reject only if too tiny
            for q in quotes[:2]:
                nq = norm_for_quote_match(str(q))
                if len(nq) < 6:
                    raise ValueError(f"{k}: quote too short.")
                if nq not in ep:
                    raise ValueError(f"{k}: quote not found in evidence pack.")

    # validate totals in payload if present
    if "points_scored" in payload or "denom_points" in payload or "hsus_0_100" in payload:
        # optional: validate if included
        ps = payload.get("points_scored")
        dp = payload.get("denom_points")
        hs = payload.get("hsus_0_100")
        # compute from provided scores, counting only non-null
        points = 0
        denom = 0
        for k in CRIT_KEYS:
            sc = crit[k].get("score", None)
            if sc in (0, 1, 2):
                # assessed criteria are those with non-null scores
                points += int(sc)
                denom += 2
        exp_hs = int(round((points / denom) * 100)) if denom > 0 else 0
        if ps is not None and ps != points:
            raise ValueError(f"points_scored mismatch: got {ps}, expected {points}.")
        if dp is not None and dp != denom:
            raise ValueError(f"denom_points mismatch: got {dp}, expected {denom}.")
        if hs is not None and hs != exp_hs:
            raise ValueError(f"hsus_0_100 mismatch: got {hs}, expected {exp_hs}.")

def llm_score(evidence_pack: str, model: str) -> Tuple[Optional[Dict[str, Any]], str]:
    if not HAS_OPENAI:
        return None, "OpenAI SDK not installed."
    if not os.getenv("OPENAI_API_KEY"):
        return None, "OPENAI_API_KEY not set."

    client = OpenAI(timeout=LLM_TIMEOUT_S, max_retries=LLM_MAX_RETRIES)

    system = (
        "You are a strict source-evaluation judge.\n"
        "RULES:\n"
        "1) Use ONLY the evidence pack. Do NOT use outside knowledge.\n"
        "2) Output VALID JSON only (no markdown).\n"
        "3) For each criterion C1..C10: provide {score, reason, evidence_quotes}.\n"
        "   - score must be 0,1,2 OR null if not assessable from the evidence pack.\n"
        "   - evidence_quotes must be 0–2 short verbatim snippets copied from the evidence pack.\n"
        "   - If quotes are missing, reason must explicitly say 'insufficient evidence'.\n"
        "4) Do not mention any organization names in your REASONS (quotes can include names).\n"
        "5) Copy quotes EXACTLY from the evidence pack.\n"
    )

    user = "EVIDENCE PACK:\n" + evidence_pack

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
        data = json.loads(out_text)
        if not isinstance(data, dict):
            raise ValueError("LLM JSON must be an object.")

        # Accept either schema:
        # (a) {"criteria": {...}}
        # (b) {"C1": {...}, "C2": {...}, ...} -> wrap
        if "criteria" not in data and all(k in data for k in CRIT_KEYS):
            data = {"criteria": {k: data[k] for k in CRIT_KEYS}}

        validate_llm_payload(data, evidence_pack)
        return data, ""

    except Exception as e:
        return None, str(e)

# -----------------------------
# Recommendation logic
# -----------------------------

def base_recommendation_from_hsus(hsus: int) -> str:
    if hsus >= 85:
        return "Preferred: primary factual support"
    if hsus >= 65:
        return "Usable with safeguards (corroborate for factual support)"
    if hsus >= 45:
        return "Context-only (not for key factual claims)"
    return "Do not use (except possibly as narrative if relevant)"

def apply_use_policies(rec: str, intended_use: str, relation: str, completeness: str, page_type: str) -> str:
    # If intended use A: narrative is acceptable even when partial/failed (but warn)
    if intended_use == "A":
        if rec.startswith("Do not use"):
            return "Narrative-only: use to quote what was said (A), not as factual proof"
        return rec

    # For B: completeness is a hard cap (prevents slip-through without punishing HSUS)
    if intended_use == "B":
        if completeness != "complete":
            return "Context-only (manual retrieval needed: incomplete/extraction/listing page)"
        # self-interest for B: cap to narrative/context
        if relation == "self":
            return "Restricted: self-interested source; use as narrative (A) or context, not independent factual proof (B)"
        # listing page: cap
        if page_type == "listing":
            return "Context-only (listing/section page; not the cited article)"

    # For C: allow context use even if partial, but mark
    if intended_use == "C":
        if completeness == "failed":
            return "Context-only (manual retrieval needed: fetch failed)"
        if relation == "self" and rec.startswith("Preferred"):
            return "Usable with safeguards (self-interested context; treat as perspective)"

    return rec

# -----------------------------
# Works cited parsing
# -----------------------------

def parse_works_cited_lines(path: str) -> List[Tuple[str, str]]:
    """
    Supports optional tab label:
      LABEL<TAB>citation...
    """
    items: List[Tuple[str, str]] = []
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

# -----------------------------
# Output
# -----------------------------

def to_json(results: List[SourceResult]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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
            "page_type": r.page_type,
            "completeness": r.completeness,
            "confidence": r.confidence,
            "gating": r.gating,
            "criteria": {
                k: {
                    "score": v.score,
                    "assessed": v.assessed,
                    "reason": v.reason,
                    "evidence_quotes": v.evidence_quotes,
                } for k, v in r.criteria.items()
            },
            "points_scored": r.points_scored,
            "denom_points": r.denom_points,
            "hsus_0_100": r.hsus_0_100,
            "recommendation": r.recommendation,
            "works_cited_entry": r.works_cited_entry,
            "evidence_pages": r.evidence_pages,
            "llm_used": r.llm_used,
            "llm_error": r.llm_error,
        })
    return out

def write_json_checkpoint(results: List[SourceResult], path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_json(results), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def write_markdown(results: List[SourceResult], path: str) -> None:
    lines: List[str] = []
    lines.append("# Source Evaluation Report\n")
    lines.append(f"_Generated: {now_utc_date()}_\n")
    lines.append(f"_Build: Source Evaluator v4 — {BUILD_ID}_\n")

    for r in results:
        lines.append(f"## {r.final_url}\n")
        if r.group_label:
            lines.append(f"- **Group:** {r.group_label}\n")
        lines.append(f"- **Domain:** {r.domain}\n")
        lines.append(f"- **Intended use:** {r.intended_use}\n")
        lines.append(f"- **Relation:** {r.relation}\n")
        lines.append(f"- **Fetch status:** {r.fetch_status} ({r.content_type or 'unknown'})\n")
        lines.append(f"- **Page type:** {r.page_type}\n")
        lines.append(f"- **Completeness:** {r.completeness}\n")
        lines.append(f"- **Confidence:** {r.confidence}\n")
        lines.append(f"- **HSUS (0–100):** {r.hsus_0_100}\n")
        lines.append(f"- **Points / Denom:** {r.points_scored} / {r.denom_points}\n")
        lines.append(f"- **Recommendation:** {r.recommendation}\n")
        lines.append(f"- **LLM used:** {r.llm_used}\n")
        if r.llm_error:
            lines.append(f"- **LLM error:** {r.llm_error}\n")

        if r.gating.get("reasons"):
            lines.append(f"- **Gate reasons:** {', '.join(r.gating['reasons'])}\n")
        if r.gating.get("warnings"):
            lines.append(f"- **Warnings:** {', '.join(r.gating['warnings'])}\n")

        if r.gating.get("auto_reject"):
            lines.append("\n---\n")
            continue

        lines.append("\n### Criteria breakdown (0–2 each; N/A excluded from denominator)\n")
        for k in CRIT_KEYS:
            c = r.criteria[k]
            sc = "N/A" if not c.assessed else str(c.score)
            lines.append(f"- **{k}: {sc}** — {c.reason}")
            if c.evidence_quotes:
                for q in c.evidence_quotes[:2]:
                    lines.append(f"  - Evidence: “{re.sub(r'\\s+', ' ', q)[:260]}”")

        lines.append("\n### Evidence pages fetched\n")
        for u in r.evidence_pages[:12]:
            lines.append(f"- {u}")
        if len(r.evidence_pages) > 12:
            lines.append(f"- (+{len(r.evidence_pages)-12} more)\n")

        lines.append("\n---\n")

    lines.append("\n# Works Cited\n")
    for r in results:
        lines.append(f"- {r.works_cited_entry}")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

# -----------------------------
# Evaluation
# -----------------------------

def evaluate(
    items: List[Tuple[str, str]],
    intended_use: str,
    relation_arg: str,
    mode: str,
    llm_model: str,
    cache_dir: str,
    sleep_s: float,
    max_aux_pages: int,
    checkpoint_path: Optional[str] = None,
) -> List[SourceResult]:
    session = requests_session()
    registry = load_domain_registry(DOMAIN_REGISTRY_PATH)
    accessed = now_utc_date()

    labeled_urls: List[Tuple[str, str]] = []
    for group, raw in items:
        for u in extract_urls_from_text(raw):
            labeled_urls.append((group, u))

    # Fetch all main docs first (for corroboration)
    main_docs: List[Tuple[str, FetchedDoc]] = []
    for group, url in labeled_urls:
        doc = fetch_doc(session, url, cache_dir, sleep_s=sleep_s)
        main_docs.append((group, doc))
    all_mains_only = [d for _, d in main_docs]

    results: List[SourceResult] = []

    for i, (group, main) in enumerate(main_docs, start=1):
        domain = get_registered_domain(main.final_url or main.url)

        # page type
        page_type = classify_page_type(main.final_url, main.html, main.text)

        # relation inference (generic)
        relation = infer_relation_from_url(main, relation_arg)

        # aux pages: always attempt when fetch ok/pdf (do not gate on text length)
        aux_pages: List[FetchedDoc] = []
        if main.fetch_status in ("ok", "pdf"):
            aux_pages = crawl_site_pages(session, main, cache_dir, sleep_s=sleep_s, max_aux_pages=max_aux_pages)

        gating = gate_source(main, aux_pages, registry, page_type)
        completeness = compute_completeness(main, page_type)

        print(f"[{i}/{len(main_docs)}] {main.final_url} status={main.fetch_status} page_type={page_type} completeness={completeness} text_len={len((main.text or '').strip())}", flush=True)

        works = format_works_cited(main, accessed)
        evidence_pages = sorted(set([main.final_url] + [p.final_url for p in aux_pages]))

        # Auto-reject ends here
        if gating.get("auto_reject"):
            criteria = {k: criterion(None, False, "Not assessed (auto-reject).", []) for k in CRIT_KEYS}
            points, denom, hsus = 0, 0, 0
            rec = "Do not use (auto-reject)"
            r = SourceResult(
                url=main.url, final_url=main.final_url, domain=domain, group_label=group,
                intended_use=intended_use, relation=relation,
                fetch_status=main.fetch_status, content_type=main.content_type, bytes_downloaded=main.bytes_downloaded,
                completeness=completeness, page_type=page_type, confidence="low", gating=gating,
                criteria=criteria, points_scored=points, denom_points=denom, hsus_0_100=hsus,
                recommendation=rec, works_cited_entry=works, evidence_pages=evidence_pages,
                llm_used=False, llm_error=""
            )
            results.append(r)
            if checkpoint_path:
                write_json_checkpoint(results, checkpoint_path)
            continue

        # Heuristic baseline
        criteria = score_heuristic(main, aux_pages, registry, all_mains_only, relation, page_type)
        llm_used = False
        llm_error = ""

        # LLM scoring
        if mode in ("llm", "hybrid"):
            evidence_pack = build_evidence_pack(main, aux_pages, page_type, completeness, relation, intended_use)

            # Don’t ask LLM if clearly failed fetch; but do allow it for partial if there is some text
            if completeness == "failed":
                gating["warnings"].append("LLM skipped: fetch failed or blocked.")
            else:
                payload, err = llm_score(evidence_pack, llm_model)
                if payload:
                    llm_used = True
                    llm_crit = payload.get("criteria", {})

                    # Apply LLM scores
                    for k in CRIT_KEYS:
                        item = llm_crit.get(k, {})
                        sc = item.get("score", None)
                        rsn = str(item.get("reason", "")).strip() or "insufficient evidence"
                        quotes = (item.get("evidence_quotes") or [])[:2]

                        # Determine assessed flag: assessed iff score is 0/1/2
                        assessed = sc in (0, 1, 2)
                        score_val = int(sc) if assessed else None

                        # Hybrid keeps C2 deterministic (relation policy)
                        if mode == "hybrid" and k == "C2":
                            continue
                        # Hybrid keeps C1 deterministic unless LLM returns a definite score
                        if mode == "hybrid" and k == "C1" and assessed is False:
                            continue

                        criteria[k] = criterion(score_val, assessed, rsn, quotes)

                else:
                    llm_error = err
                    gating["warnings"].append(f"LLM failed/invalid; using heuristics. ({err})")

        # Compute HSUS from assessed criteria
        points, denom, hsus = compute_points(criteria)
        rec = base_recommendation_from_hsus(hsus)

        # Apply use policies (completeness/relation)
        rec = apply_use_policies(rec, intended_use, relation, completeness, page_type)

        confidence = confidence_from_completeness(completeness, llm_used)

        r = SourceResult(
            url=main.url, final_url=main.final_url, domain=domain, group_label=group,
            intended_use=intended_use, relation=relation,
            fetch_status=main.fetch_status, content_type=main.content_type, bytes_downloaded=main.bytes_downloaded,
            completeness=completeness, page_type=page_type, confidence=confidence, gating=gating,
            criteria=criteria, points_scored=points, denom_points=denom, hsus_0_100=hsus,
            recommendation=rec, works_cited_entry=works, evidence_pages=evidence_pages,
            llm_used=llm_used, llm_error=llm_error
        )
        results.append(r)

        if checkpoint_path:
            write_json_checkpoint(results, checkpoint_path)

    return results

# -----------------------------
# CLI
# -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--works-cited", default="", help="Path to Works Cited text file (any format).")
    ap.add_argument("--out-md", default="report_v4.md", help="Markdown report path.")
    ap.add_argument("--out-json", default="report_v4.json", help="JSON output path.")
    ap.add_argument("--intended-use", choices=["A", "B", "C"], default="B")
    ap.add_argument("--relation", choices=["auto", "self", "adversary", "third_party", "non_political_fact", "unknown"], default="auto")
    ap.add_argument("--mode", choices=["heuristic", "llm", "hybrid"], default="hybrid")
    ap.add_argument("--llm-model", default="gpt-5.2")
    ap.add_argument("--cache-dir", default=".cache_sources_v4")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_S)
    ap.add_argument("--max-aux-pages", type=int, default=DEFAULT_MAX_AUX_PAGES)
    ap.add_argument("--checkpoint", default="", help="Optional checkpoint JSON path (writes progress after each source).")
    args = ap.parse_args()

    if not args.works_cited:
        raise SystemExit("Provide --works-cited <file>.")

    items = parse_works_cited_lines(args.works_cited)
    if not items:
        raise SystemExit("No usable lines found in works cited file.")

    checkpoint_path = args.checkpoint or (args.out_json + ".partial")

    results = evaluate(
        items=items,
        intended_use=args.intended_use,
        relation_arg=args.relation,
        mode=args.mode,
        llm_model=args.llm_model,
        cache_dir=args.cache_dir,
        sleep_s=args.sleep,
        max_aux_pages=args.max_aux_pages,
        checkpoint_path=checkpoint_path,
    )

    write_markdown(results, args.out_md)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(to_json(results), f, ensure_ascii=False, indent=2)

    print(f"Wrote: {args.out_md}")
    print(f"Wrote: {args.out_json}")
    print(f"Checkpoint: {checkpoint_path}")

if __name__ == "__main__":
    main()

