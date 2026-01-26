#!/usr/bin/env python3
"""
Source Evaluator v3.1 (HSUS 0–100) — BUILD 2026-01-25b

Core behavior:
- Input: Works Cited text (any format) OR URLs.
- Fetch: main URL + (optional) site policy pages (about/standards/corrections/etc.).
- Evidence-driven: score ONLY from fetched text; no outside knowledge.
- Fetchability ≠ credibility: paywall/bot-block/timeout -> low confidence + capped recommendations.
- Intended use:
  A = official narrative (“what they claim”)
  B = factual support (“what happened”)
  C = analytic context (“background/interpretation”)
- Rubric: C1..C10 scored 0–2 -> Total 0–20 -> HSUS 0–100.
- Gating:
  - Auto-REJECT only for clear junk/unverifiable origin or truly-unretrievable (404/410/451), satire, known spam.
  - Auto-RESTRICT state/party/official sources only when relation=self (self-interest): narrative A-only.
- LLM scoring (optional):
  - JSON-mode output, evidence quotes must match evidence pack (robust normalization).
  - If LLM stalls/fails/invalid -> fall back to heuristics and continue (no hangs).

Operational hardening:
- Requests connect/read timeouts (prevents network stalls).
- PDF text extraction in subprocess w/ timeout (prevents pdfminer hangs).
- OpenAI client timeouts + retries (prevents indefinite waits).
- Incremental checkpoint JSON write (so long runs still produce output).
"""

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
import warnings
import multiprocessing as mp
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Literal
from urllib.parse import urlparse, urljoin

import requests
import tldextract
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dateutil import parser as dateparser

# Optional: readability
try:
    from readability import Document
    HAS_READABILITY = True
except Exception:
    HAS_READABILITY = False

# Optional: pdfminer
try:
    from pdfminer.high_level import extract_text as _pdf_extract_text
    HAS_PDFMINER = True
except Exception:
    HAS_PDFMINER = False

# Optional: OpenAI
try:
    from openai import OpenAI
    HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

# Silence bs4 XML warning + pdfminer noise
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# -----------------------------
# Configuration
# -----------------------------

DEFAULT_TIMEOUT_CONNECT = 8
DEFAULT_TIMEOUT_READ = 20
DEFAULT_SLEEP_S = 0.8

USER_AGENT = "SourceEvaluatorBot/3.1 (+contact: research@yourcompany.example)"
DOMAIN_REGISTRY_PATH = "domain_registry.json"

CRAWL_PATHS = [
    "/about", "/about-us", "/contact", "/contact-us",
    "/editorial-policy", "/ethics", "/standards", "/values", "/principles",
    "/methods", "/methodology",
    "/corrections", "/correction", "/retractions",
    "/terms", "/privacy", "/policies"
]

SATIRE_KEYWORDS = ["satire", "parody", "humor", "humour", "comedy", "entertainment"]
KNOWN_SATIRE_DOMAINS = {"theonion.com", "babylonbee.com", "clickhole.com"}

KNOWN_BAD_DOMAINS = set()  # optional local blocklist

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

HIGH_STAKES_HARM_TERMS = [
    "torture", "killed", "executed", "massacre", "disappeared", "disappearance",
    "imprisoned", "detained", "arrested", "kidnapped", "abducted",
    "rape", "sexual violence", "forced labor", "forced labour",
    "genocide", "ethnic cleansing", "crimes against humanity",
]

SYSTEMATICITY_TERMS = ["systematic", "widespread", "routine", "pattern", "regularly", "dozens", "hundreds", "thousands", "daily", "weekly"]
INSTITUTION_TERMS = ["law", "decree", "directive", "policy", "regulation", "ministry", "agency", "security service", "court order", "statute"]

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
    group_label: str
    intended_use: str
    relation: str
    fetch_status: str
    content_type: str
    bytes_downloaded: int

    confidence: str
    high_stakes: bool
    severity_support: Dict[str, Any]

    gating: Dict[str, Any]
    criteria: Dict[str, Criterion]
    total_0_20: int
    hsus_0_100: int
    recommendation: str
    works_cited_entry: str
    evidence_pages: List[str]
    llm_used: bool
    llm_error: str = ""

# -----------------------------
# Helpers
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

def score_0_2(v: int) -> int:
    return max(0, min(2, int(v)))

def assess_high_stakes(text: str) -> bool:
    low = (text or "").lower()
    return any(t in low for t in HIGH_STAKES_HARM_TERMS)

def assess_severity_support(text: str) -> Dict[str, Any]:
    low = (text or "").lower()
    extent = any(t in low for t in HIGH_STAKES_HARM_TERMS)
    systematicity = any(t in low for t in SYSTEMATICITY_TERMS)
    institutional = any(t in low for t in INSTITUTION_TERMS)
    missing = []
    if not extent:
        missing.append("extent")
    if not systematicity:
        missing.append("systematicity")
    if not institutional:
        missing.append("institutionalization")
    return {
        "extent": extent,
        "systematicity": systematicity,
        "institutionalization": institutional,
        "supports_severity_coding": (extent and systematicity and institutional),
        "missing": missing,
    }

def assess_confidence(main: FetchedDoc, aux_pages: List[FetchedDoc], llm_used: bool) -> str:
    if main.fetch_status in ("timeout", "blocked", "paywall", "pdf_no_parser"):
        return "low"
    text_len = len((main.text or "").strip())
    aux_ok = any(len((p.text or "").strip()) > 400 for p in aux_pages)
    if text_len >= 1200 and (aux_ok or llm_used):
        return "high"
    if text_len >= 400:
        return "medium"
    return "low"

# -----------------------------
# Text extraction
# -----------------------------

def extract_main_text(html: str, soup: BeautifulSoup) -> str:
    if HAS_READABILITY and html:
        try:
            doc = Document(html)
            cleaned = doc.summary()
            soup2 = BeautifulSoup(cleaned, "lxml")
            return soup2.get_text("\n", strip=True)
        except Exception:
            pass

    for tag_name in ("article", "main"):
        tag = soup.find(tag_name)
        if tag:
            for bad in tag(["script", "style", "noscript"]):
                bad.decompose()
            txt = tag.get_text("\n", strip=True)
            if len(txt) >= 300:
                return txt

    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()
    return soup.get_text("\n", strip=True)

# -----------------------------
# PDF extraction with timeout
# -----------------------------

def _pdf_worker(pdf_path: str, q: mp.Queue) -> None:
    try:
        from pdfminer.high_level import extract_text as pdf_extract_text
        q.put(pdf_extract_text(pdf_path) or "")
    except Exception:
        q.put("")

def extract_pdf_text_with_timeout(pdf_path: str, timeout_s: int = 25) -> Tuple[str, str]:
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
# Fetching + parsing
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
            pass  # refetch

    final_url = url
    status = 0
    content_type = ""
    raw = b""
    fetch_status = "unknown"
    error = ""

    try:
        time.sleep(max(0.0, float(sleep_s)))
        r = session.get(
            url,
            timeout=(DEFAULT_TIMEOUT_CONNECT, DEFAULT_TIMEOUT_READ),
            allow_redirects=True,
        )
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

        pdf_text, pdf_err = extract_pdf_text_with_timeout(tmp_path, timeout_s=25)
        fetch_status = "pdf" if HAS_PDFMINER else "pdf_no_parser"
        doc = FetchedDoc(
            url=url, final_url=final_url, status_code=status,
            fetch_status=fetch_status, content_type=content_type,
            bytes_downloaded=bytes_downloaded, text=pdf_text, error=pdf_err
        )
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
        return doc

    html = sanitize_html(raw.decode(errors="replace"))
    html_excerpt = clip(html, 250000)

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
        site_name=site_name, meta=meta, error=""
    )
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(doc), f, ensure_ascii=False)
    return doc

def discover_policy_links_from_html(base_url: str, html: str, max_links: int = 10) -> List[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    keywords = (
        "about", "standards", "ethics", "editorial", "values", "principles",
        "corrections", "retractions", "policy", "policies", "method", "methodology",
        "who we are", "our mission", "governance", "independent"
    )
    links: List[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        txt = (a.get_text(" ", strip=True) or "").lower()
        hlow = href.lower()
        if any(k in txt for k in keywords) or any(k in hlow for k in keywords):
            links.append(urljoin(base_url, href))

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

def crawl_site_pages(session: requests.Session, main: FetchedDoc, cache_dir: str, sleep_s: float, max_aux_pages: int = 6) -> List[FetchedDoc]:
    pages: List[FetchedDoc] = []
    if not main.final_url:
        return pages

    parsed = urlparse(main.final_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    # Probe standard paths
    for path in CRAWL_PATHS:
        doc = fetch_doc(session, urljoin(root, path), cache_dir, sleep_s)
        if doc.fetch_status in ("ok", "pdf", "xml") and len((doc.text or "")) > 200:
            pages.append(doc)
        if len(pages) >= max_aux_pages:
            break

    if len(pages) < max_aux_pages:
        discovered = discover_policy_links_from_html(main.final_url, main.html or "", max_links=12)
        for u in discovered:
            doc = fetch_doc(session, u, cache_dir, sleep_s)
            if doc.fetch_status in ("ok", "pdf", "xml") and len((doc.text or "")) > 200:
                pages.append(doc)
            if len(pages) >= max_aux_pages:
                break

    # Dedup by final_url
    dedup: Dict[str, FetchedDoc] = {}
    for p in pages:
        dedup[p.final_url] = p
    return list(dedup.values())

# -----------------------------
# Relation + gating
# -----------------------------

def infer_relation(domain: str, intended_use: str, relation_arg: str) -> str:
    if relation_arg and relation_arg != "auto":
        return relation_arg
    if domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn"):
        return "self" if intended_use in ("A", "B") else "unknown"
    return "unknown"

def gate_source(main: FetchedDoc, aux_pages: List[FetchedDoc], registry: Dict[str, Any], intended_use: str, relation: str) -> Dict[str, Any]:
    domain = get_registered_domain(main.final_url or main.url)
    reg = registry.get(domain, {})

    reasons: List[str] = []
    warnings_list: List[str] = []
    auto_reject = False
    auto_restrict = False
    restrict_reason = ""

    # Fetch incomplete warning (not auto-reject)
    if main.fetch_status in ("timeout", "blocked", "paywall", "pdf_no_parser"):
        warnings_list.append(f"Fetch incomplete: {main.fetch_status}. Manual retrieval recommended for decisive use.")

    # True-unretrievable
    if main.fetch_status == "http_error" and main.status_code in (404, 410, 451):
        reasons.append(f"Unretrievable (HTTP {main.status_code}).")
        auto_reject = True

    combined = " ".join([
        main.title or "",
        main.site_name or "",
        (main.text or "")[:3000],
        " ".join((p.text or "")[:1200] for p in aux_pages[:3]),
    ]).lower()

    if domain in KNOWN_SATIRE_DOMAINS or reg.get("satire_publisher", False):
        reasons.append("Satire/parody publisher.")
        auto_reject = True

    if (("satire" in combined or "parody" in combined) and any(k in combined for k in SATIRE_KEYWORDS)):
        reasons.append("Page indicates satire/parody/entertainment-only.")
        auto_reject = True

    if domain in KNOWN_BAD_DOMAINS or reg.get("known_bad", False):
        reasons.append("Known bad/misinfo/spam domain.")
        auto_reject = True

    # Unverifiable origin (strict multi-signal)
    meta_thin = (not main.site_name) and (not main.author) and (not main.published_date)
    content_thin = len((main.text or "").strip()) < 250
    no_aux = len(aux_pages) == 0
    if meta_thin and content_thin and no_aux:
        reasons.append("Unverifiable origin: insufficient publisher/metadata and minimal content.")
        auto_reject = True

    # Spam/synthetic multi-signal
    spam_signals = 0
    if len((main.text or "")) < 200:
        spam_signals += 1
    if not main.title:
        spam_signals += 1
    if no_aux:
        spam_signals += 1
    if re.search(r'\b(lorem ipsum|casino|crypto giveaway|porn|adult)\b', combined):
        spam_signals += 2
    if re.search(r'\b(ai generated|generated by ai|chatgpt)\b', combined) and ("editor" not in combined and "review" not in combined):
        spam_signals += 2

    is_official_domain = domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn")
    if spam_signals >= 4 and not is_official_domain:
        reasons.append("Likely low-credibility spam/synthetic content (multiple signals).")
        auto_reject = True

    # Auto-restrict official/state/party only when self-interest
    is_state_controlled = (
        is_official_domain
        or reg.get("state_owned", False)
        or reg.get("party_owned", False)
        or reg.get("state_media", False)
    )
    if is_state_controlled and relation == "self":
        auto_restrict = True
        restrict_reason = "State/party/official source in self-interest context: use only as official narrative (A), not independent factual proof."

    if auto_restrict and intended_use == "B":
        warnings_list.append("Intended use is B but source is restricted to A due to self-interest.")

    return {
        "auto_reject": auto_reject,
        "reasons": reasons,
        "warnings": warnings_list,
        "auto_restrict": auto_restrict,
        "restrict_reason": restrict_reason,
    }

# -----------------------------
# Heuristic scoring (C1–C10)
# -----------------------------

def score_criteria_heuristic(
    main: FetchedDoc,
    aux_pages: List[FetchedDoc],
    registry: Dict[str, Any],
    all_main: List[FetchedDoc],
    intended_use: str,
    relation: str,
) -> Dict[str, Criterion]:
    domain = get_registered_domain(main.final_url or main.url)
    reg = registry.get(domain, {})

    aux_text = "\n".join([p.text for p in aux_pages if p.text])
    combined_text = "\n".join([main.title or "", main.site_name or "", main.text or "", aux_text])
    low = combined_text.lower()

    crit: Dict[str, Criterion] = {}

    # C1 Ownership/control (default strict 0 unless evidenced)
    c1 = 0
    c1_reason = "Insufficient evidence on ownership/control from fetched pages."
    c1_quotes: List[str] = []
    if reg.get("state_owned") or reg.get("party_owned") or reg.get("state_media") or domain.endswith(".gov") or domain.endswith(".mil") or domain.endswith(".gov.cn"):
        c1 = 0
        c1_reason = "Signals indicate state/party/official control."
    elif reg.get("independent", False) or re.search(r'\beditorial independence\b', low):
        c1 = 2
        c1_reason = "Signals indicate editorial independence."
        c1_quotes = extract_snippets(aux_text or combined_text, r'editorial independence|independent', 2)
    crit["C1"] = Criterion(score_0_2(c1), c1_reason, c1_quotes[:2])

    # C2 COI vs claim (relation-aware)
    if relation == "self":
        c2, c2_reason = 0, "Publisher has direct stake (self-interest context)."
    elif relation == "adversary":
        c2, c2_reason = 1, "Publisher may have material incentives (adversarial context)."
    elif relation in ("third_party", "non_political_fact"):
        c2, c2_reason = 2, "No direct stake indicated for this claim relationship."
    else:
        c2, c2_reason = 1, "Claim relationship unknown; treat as potentially interested."
    crit["C2"] = Criterion(score_0_2(c2), c2_reason, [])

    # C3 Evidence strength
    if main.fetch_status == "pdf":
        crit["C3"] = Criterion(2, "Primary evidence format (PDF) extracted.", extract_snippets(main.text, r'\b(court|law|statute|dataset|table|appendix|exhibit)\b', 2))
    elif re.search(r'\b(court|judge|filing|indictment|statute|law|dataset|data|transcript|document)\b', low):
        crit["C3"] = Criterion(2, "Primary-evidence indicators present (documents/data/transcripts).", extract_snippets(combined_text, r'\b(court|judge|filing|dataset|transcript|document)\b', 2))
    elif re.search(r'\b(according to|reported by|source:|said)\b', low) or re.search(r'https?://', main.html or ""):
        crit["C3"] = Criterion(1, "Secondary reporting with attribution/references detected.", extract_snippets(combined_text, r'\b(according to|reported|said|source:)\b', 2))
    else:
        crit["C3"] = Criterion(0, "No clear evidence trail detected in fetched text.", [])

    # C4 Method transparency
    if re.search(r'\b(method|methodology|how we reported|we interviewed|we reviewed|we analyzed|we analysed)\b', low):
        crit["C4"] = Criterion(2, "Method/verification language present.", extract_snippets(combined_text, r'\b(method|methodology|how we reported|we interviewed|we reviewed|we analyzed|we analysed)\b', 2))
    elif len(aux_pages) > 0 and re.search(r'\b(standards|ethics|corrections|retractions|editorial)\b', aux_text.lower()):
        crit["C4"] = Criterion(1, "Site-level standards/policy pages found; item-level method not explicit.", extract_snippets(aux_text, r'\b(standards|ethics|editorial|corrections|retractions)\b', 2))
    else:
        crit["C4"] = Criterion(0, "No method/verification described.", [])

    # C5 Specificity/auditability
    date_hits = len(re.findall(r'\b(20\d{2}|19\d{2})\b', main.text or ""))
    number_hits = len(re.findall(r'\b\d{2,}\b', main.text or ""))
    if len((main.text or "")) < 250:
        crit["C5"] = Criterion(0, "Vague/minimal extracted text; poor auditability.", [])
    elif date_hits >= 2 and number_hits >= 4 and len((main.text or "")) > 800:
        crit["C5"] = Criterion(2, "High specificity: dates/numbers/actors present.", extract_snippets(main.text, r'\b(20\d{2}|19\d{2})\b|\b\d{2,}\b', 2))
    else:
        crit["C5"] = Criterion(1, "Some specifics present; audit trail may be incomplete.", extract_snippets(main.text, r'\b(20\d{2}|19\d{2})\b|\b\d{2,}\b', 2))

    # C6 Corroboration (within set) — N/A if only one
    if len(all_main) < 2:
        crit["C6"] = Criterion(0, "Not assessed (only one source in set).", [])
    else:
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
            crit["C6"] = Criterion(2, "Likely corroborated by multiple independent sources in set.", [])
        elif matches == 1:
            crit["C6"] = Criterion(1, "Likely corroborated by at least one independent source in set.", [])
        else:
            crit["C6"] = Criterion(0, "No corroboration detected within provided set.", [])

    # C7 Legal/institutional confirmation
    if re.search(r'\b(ruled|found|convicted|sentenced)\b', low) and re.search(r'\b(court|judge)\b', low):
        crit["C7"] = Criterion(2, "Confirmed by legal/institutional process language.", extract_snippets(combined_text, r'\b(court|judge|ruled|found|convicted|sentenced)\b', 2))
    elif re.search(r'\b(court|judge|ruling|verdict|indictment|charges filed|case number|filing)\b', low):
        crit["C7"] = Criterion(1, "Legal/institutional process referenced; confirmation may be partial.", extract_snippets(combined_text, r'\b(court|judge|ruling|verdict|indictment|charges filed|case number|filing)\b', 2))
    else:
        crit["C7"] = Criterion(0, "No legal/institutional confirmation signals detected.", [])

    # C8 Track record/corrections (default strict 0 unless evidenced)
    has_corrections = any(
        ("correction" in (p.final_url or "").lower())
        or ("retraction" in (p.final_url or "").lower())
        or re.search(r'\b(corrections|retractions|we correct)\b', (p.text or "").lower())
        for p in aux_pages
    )
    if reg.get("frequent_misinfo", False) or reg.get("known_bad", False):
        crit["C8"] = Criterion(0, "Registry signals frequent misinformation or no correction behavior.", [])
    elif has_corrections or re.search(r'\b(corrections policy|we correct|retractions)\b', low):
        crit["C8"] = Criterion(2, "Corrections/retractions behavior indicated.", extract_snippets(aux_text or combined_text, r'\b(corrections|retractions|we correct)\b', 2))
    elif reg.get("tertiary_reference", False):
        crit["C8"] = Criterion(1, "Tertiary/reference source; track record depends on downstream sources.", [])
    else:
        crit["C8"] = Criterion(0, "Insufficient evidence on corrections/track record.", [])

    # C9 Nuance/bias handling
    hedge = len(re.findall(r'\b(alleged|reportedly|may|might|unclear|according to)\b', low))
    absolutes = len(re.findall(r'\b(always|never|everyone|no one|obviously|undeniable)\b', low))
    if hedge >= 5 and absolutes <= 1:
        crit["C9"] = Criterion(2, "Attribution/hedging suggests nuance and uncertainty handling.", extract_snippets(combined_text, r'\b(alleged|reportedly|may|might|unclear|according to)\b', 2))
    elif absolutes >= 4 and hedge == 0:
        crit["C9"] = Criterion(0, "Absolutist framing with little uncertainty.", extract_snippets(combined_text, r'\b(always|never|everyone|no one|obviously|undeniable)\b', 2))
    else:
        crit["C9"] = Criterion(1, "Some nuance/uncertainty handling; may be incomplete.", extract_snippets(combined_text, r'\b(alleged|reportedly|according to)\b', 2))

    # C10 Domain competence
    if re.search(r'\b(forensic|dataset|method|analysis|we reviewed|we analyzed|we analysed)\b', low) and re.search(r'\b(surveillance|metadata|sanctions|beneficial owner|shell company|blockchain|forensic)\b', low):
        crit["C10"] = Criterion(2, "Specialized domain signals + method present.", extract_snippets(combined_text, r'\b(surveillance|metadata|sanctions|beneficial owner|shell company|blockchain|forensic)\b', 2))
    elif re.search(r'\b(ai|blockchain|kleptocracy|surveillance)\b', low) and not re.search(r'\b(method|dataset|analysis|we reviewed|we analyzed|we analysed)\b', low):
        crit["C10"] = Criterion(0, "Buzzword-heavy with limited method/evidence signals.", extract_snippets(combined_text, r'\b(ai|blockchain|kleptocracy|surveillance)\b', 2))
    else:
        crit["C10"] = Criterion(1, "Generalist coverage / unclear domain depth.", [])

    return crit

# -----------------------------
# LLM scoring (JSON mode + strict validation)
# -----------------------------

LLM_KEYS = ["C1","C2","C3","C4","C5","C6","C7","C8","C9","C10"]

def build_evidence_pack(main: FetchedDoc, aux_pages: List[FetchedDoc]) -> str:
    parts: List[str] = []
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
    parts.append(clip(main.text or "", 12000))

    parts.append("\n=== SITE/ORG PAGES (clipped) ===")
    for p in aux_pages[:6]:
        parts.append(f"\n--- {p.final_url} ---")
        parts.append(clip(p.text or "", 3500))

    return "\n".join(parts)

def validate_llm_payload(payload: Dict[str, Any], evidence_pack: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError("LLM payload is not an object.")
    if "criteria" not in payload or not isinstance(payload["criteria"], dict):
        raise ValueError("LLM payload missing 'criteria' dict.")

    crit = payload["criteria"]
    ep = norm_for_quote_match(evidence_pack)

    for k in LLM_KEYS:
        if k not in crit or not isinstance(crit[k], dict):
            raise ValueError(f"Missing/invalid {k}.")
        item = crit[k]
        sc = item.get("score")
        if sc not in (0, 1, 2):
            raise ValueError(f"{k}.score must be 0/1/2.")
        reason = (item.get("reason") or "").strip()
        quotes = item.get("evidence_quotes", [])
        if quotes and not isinstance(quotes, list):
            raise ValueError(f"{k}.evidence_quotes must be a list.")

        # Quotes required unless "insufficient evidence"
        allow_no_quotes = {"C1", "C2", "C8"}
        if not quotes:
            if "insufficient" not in reason.lower():
                # for these criteria, allow empty quotes only if reason admits insufficiency
                if k in allow_no_quotes:
                    raise ValueError(f"{k}: if no quotes, reason must say 'insufficient evidence'.")
                raise ValueError(f"{k}: missing quotes; reason must say 'insufficient evidence'.")
        else:
            for q in quotes[:2]:
                nq = norm_for_quote_match(str(q))
                if len(nq) < 8:
                    raise ValueError(f"{k}: quote too short.")
                if nq not in ep:
                    raise ValueError(f"{k}: quote not found in evidence pack (must be verbatim).")

    total = payload.get("total_0_20")
    hsus = payload.get("hsus_0_100")
    calc_total = sum(int(crit[k]["score"]) for k in LLM_KEYS)
    if total != calc_total:
        raise ValueError(f"total_0_20 mismatch: got {total}, expected {calc_total}.")
    if hsus != calc_total * 5:
        raise ValueError(f"hsus_0_100 mismatch: got {hsus}, expected {calc_total*5}.")

def llm_score(evidence_pack: str, model: str, intended_use: str, relation: str) -> Tuple[Optional[Dict[str, Any]], str]:
    if not HAS_OPENAI:
        return None, "OpenAI SDK not installed."
    if not os.getenv("OPENAI_API_KEY"):
        return None, "OPENAI_API_KEY not set."

    # Hard timeouts + retries (prevents indefinite hangs)
    client = OpenAI(timeout=60.0, max_retries=2)

    system = (
        "You are a strict source-evaluation judge.\n"
        "RULES:\n"
        "1) Use ONLY the provided evidence pack. Do NOT use outside knowledge.\n"
        "2) Return VALID JSON only (no markdown).\n"
        "3) Output schema:\n"
        "{\n"
        '  "criteria": {\n'
        '    "C1": {"score":0..2,"reason":"...","evidence_quotes":["..."]},\n'
        "    ... C10 ...\n"
        "  },\n"
        '  "total_0_20": int,\n'
        '  "hsus_0_100": int,\n'
        '  "notes": "optional"\n'
        "}\n"
        "4) Provide 1–2 verbatim quotes per criterion when possible.\n"
        "   If missing, write 'insufficient evidence' and use [] for evidence_quotes.\n"
        "5) Ensure totals match: total_0_20 = sum scores, hsus_0_100 = total_0_20*5.\n"
        "6) Do not mention any organization names in your reasoning (quotes are allowed).\n"
        "7) Copy quotes EXACTLY from the evidence pack. Use straight quotes.\n"
    )
    rubric_ctx = (
        f"Context:\n- intended_use: {intended_use} (A/B/C)\n"
        f"- relation: {relation} (self/adversary/third_party/non_political_fact/unknown)\n"
        "Rubric (0–2 each): C1 ownership, C2 COI, C3 evidence, C4 method, C5 specificity, "
        "C6 corroboration (ONLY from evidence pack), C7 legal/institutional, C8 track record, "
        "C9 nuance, C10 domain competence.\n"
    )
    user = rubric_ctx + "\nEVIDENCE PACK:\n" + evidence_pack

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
        validate_llm_payload(data, evidence_pack)
        return data, ""
    except Exception as e:
        return None, str(e)

# -----------------------------
# Recommendation mapping + policy caps
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
    if intended_use == "A":
        if rec.startswith("Do not use"):
            return "Narrative-only: use to quote what was said (A), not as factual proof"
        return rec
    return rec

def enforce_b_hard_floors(rec: str, confidence: str, criteria: Dict[str, Criterion], relation: str) -> str:
    if confidence == "low":
        return "Context-only (low confidence: incomplete access/extraction)"
    if relation == "unknown" and rec.startswith("Preferred"):
        return "Usable with safeguards (relation unknown; treat as potentially interested)"
    if criteria.get("C3", Criterion(0,"",[])).score == 0 and rec.startswith("Preferred"):
        return "Usable with safeguards (insufficient evidence trail for Preferred)"
    # If corroboration not assessed, never allow Preferred for B
    if criteria.get("C6", Criterion(0,"",[])).reason.startswith("Not assessed") and rec.startswith("Preferred"):
        return "Usable with safeguards (corroboration not assessed in this run)"
    return rec

def enforce_high_stakes_policy(intended_use: str, high_stakes: bool, criteria: Dict[str, Criterion], hsus: int, rec: str) -> str:
    if intended_use != "B" or not high_stakes:
        return rec
    c6 = criteria.get("C6", Criterion(0,"",[])).score
    c3 = criteria.get("C3", Criterion(0,"",[])).score
    c7 = criteria.get("C7", Criterion(0,"",[])).score
    anchor_ok = (c3 == 2) or (c7 >= 1)
    if hsus >= 85:
        return rec
    if hsus >= 65 and c6 >= 1 and anchor_ok:
        if rec.startswith("Preferred"):
            return "Usable with safeguards (high-stakes: corroboration + anchor required)"
        if rec.startswith("Usable"):
            return "Usable with safeguards (high-stakes: corroboration + anchor required)"
        return rec
    return "Context-only (high-stakes claim: requires stronger corroboration/anchors)"

# -----------------------------
# Works cited parsing
# -----------------------------

def parse_works_cited_lines(path: str) -> List[Tuple[str, str]]:
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
# Output helpers
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
            "confidence": r.confidence,
            "high_stakes": r.high_stakes,
            "severity_support": r.severity_support,
            "gating": r.gating,
            "criteria": {
                k: {"score": v.score, "reason": v.reason, "evidence_quotes": v.evidence_quotes}
                for k, v in r.criteria.items()
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

def write_json_checkpoint(results: List[SourceResult], path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_json(results), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def write_markdown(results: List[SourceResult], path: str) -> None:
    lines: List[str] = []
    lines.append("# Source Evaluation Report\n")
    lines.append(f"_Generated: {now_utc_date()}_\n")
    lines.append("_Build: Source Evaluator v3.1 — 2026-01-25b_\n")

    for r in results:
        lines.append(f"## {r.final_url}\n")
        if r.group_label:
            lines.append(f"- **Group:** {r.group_label}\n")
        lines.append(f"- **Domain:** {r.domain}\n")
        lines.append(f"- **Intended use:** {r.intended_use}\n")
        lines.append(f"- **Relation:** {r.relation}\n")
        lines.append(f"- **Fetch status:** {r.fetch_status} ({r.content_type or 'unknown'})\n")
        lines.append(f"- **Confidence:** {r.confidence}\n")
        lines.append(f"- **High-stakes claim detected:** {r.high_stakes}\n")
        lines.append(f"- **Severity coding support:** {r.severity_support.get('supports_severity_coding', False)}\n")
        if r.severity_support.get("missing"):
            lines.append(f"- **Severity missing:** {', '.join(r.severity_support['missing'])}\n")
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
# Evaluation pipeline
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
    checkpoint_json_path: Optional[str] = None
) -> List[SourceResult]:
    session = requests_session()
    registry = load_domain_registry(DOMAIN_REGISTRY_PATH)
    accessed = now_utc_date()

    labeled_urls: List[Tuple[str, str]] = []
    for group, raw in items:
        for u in extract_urls_from_text(raw):
            labeled_urls.append((group, u))

    # Fetch all main docs first (needed for C6)
    main_docs: List[Tuple[str, FetchedDoc]] = []
    for idx, (group, url) in enumerate(labeled_urls):
        doc = fetch_doc(session, url, cache_dir, sleep_s)
        main_docs.append((group, doc))

    all_mains_only = [d for _, d in main_docs]
    results: List[SourceResult] = []

    for i, (group, main) in enumerate(main_docs, start=1):
        print(f"[{i}/{len(main_docs)}] {main.final_url} ({main.fetch_status})", flush=True)

        domain = get_registered_domain(main.final_url or main.url)
        relation = infer_relation(domain, intended_use, relation_arg)

        # Skip aux crawling if main is clearly incomplete or tiny (reduces blocks/stalls)
        aux_pages: List[FetchedDoc] = []
        if main.fetch_status == "ok" and len((main.text or "").strip()) > 300:
            aux_pages = crawl_site_pages(session, main, cache_dir, sleep_s, max_aux_pages=max_aux_pages)

        gating = gate_source(main, aux_pages, registry, intended_use, relation)

        high_stakes = assess_high_stakes(main.text or "")
        severity = assess_severity_support(main.text or "")

        if gating["auto_reject"]:
            works = format_works_cited(main, accessed)
            r = SourceResult(
                url=main.url, final_url=main.final_url, domain=domain,
                group_label=group, intended_use=intended_use, relation=relation,
                fetch_status=main.fetch_status, content_type=main.content_type,
                bytes_downloaded=main.bytes_downloaded,
                confidence="low", high_stakes=high_stakes, severity_support=severity,
                gating=gating, criteria={}, total_0_20=0, hsus_0_100=0,
                recommendation="Do not use (auto-reject)",
                works_cited_entry=works,
                evidence_pages=sorted(set([main.final_url] + [p.final_url for p in aux_pages])),
                llm_used=False, llm_error=""
            )
            results.append(r)
            if checkpoint_json_path:
                write_json_checkpoint(results, checkpoint_json_path)
            continue

        # Heuristic baseline
        criteria = score_criteria_heuristic(main, aux_pages, registry, all_mains_only, intended_use, relation)

        llm_used = False
        llm_error = ""

        # LLM optional
        if mode in ("llm", "hybrid"):
            evidence_pack = build_evidence_pack(main, aux_pages)
            if len((main.text or "").strip()) < 350 or main.fetch_status in ("timeout", "blocked", "paywall", "pdf_no_parser"):
                gating["warnings"].append("LLM skipped due to insufficient fetched evidence (or blocked/paywalled).")
            else:
                payload, err = llm_score(evidence_pack, llm_model, intended_use, relation)
                if payload:
                    llm_used = True
                    llm_crit = payload["criteria"]
                    # hybrid keeps C1/C2 deterministic
                    for k in LLM_KEYS:
                        if mode == "hybrid" and k in ("C1", "C2"):
                            continue
                        item = llm_crit[k]
                        criteria[k] = Criterion(
                            score=int(item["score"]),
                            reason=str(item.get("reason", "")).strip(),
                            evidence_quotes=(item.get("evidence_quotes") or [])[:2]
                        )
                else:
                    llm_error = err
                    gating["warnings"].append(f"LLM failed/invalid; fell back to heuristics. ({err})")

        # Score
        total = max(0, min(20, sum(c.score for c in criteria.values())))
        hsus = total * 5
        rec = recommendation_from_hsus(hsus)

        # Apply restriction
        if gating.get("auto_restrict"):
            rec = "Restricted: official position/narrative only (A)"

        confidence = assess_confidence(main, aux_pages, llm_used)

        # Intended-use policy
        rec = apply_intended_use_policy(rec, intended_use)

        # B-use caps
        if intended_use == "B" and not gating.get("auto_restrict"):
            rec = enforce_b_hard_floors(rec, confidence, criteria, relation)
            rec = enforce_high_stakes_policy(intended_use, high_stakes, criteria, hsus, rec)

        # Severity support warning (does not change HSUS, but tells analysts not to over-generalize)
        if intended_use == "B" and high_stakes and not severity.get("supports_severity_coding", False):
            gating["warnings"].append(
                "Severity support incomplete: avoid broad conclusions about systematic/widespread/state policy "
                f"(missing: {', '.join(severity.get('missing', []))})."
            )

        works = format_works_cited(main, accessed)
        evidence_pages = sorted(set([main.final_url] + [p.final_url for p in aux_pages]))

        r = SourceResult(
            url=main.url, final_url=main.final_url, domain=domain,
            group_label=group, intended_use=intended_use, relation=relation,
            fetch_status=main.fetch_status, content_type=main.content_type,
            bytes_downloaded=main.bytes_downloaded,
            confidence=confidence, high_stakes=high_stakes, severity_support=severity,
            gating=gating, criteria=criteria,
            total_0_20=total, hsus_0_100=hsus,
            recommendation=rec,
            works_cited_entry=works,
            evidence_pages=evidence_pages,
            llm_used=llm_used, llm_error=llm_error
        )
        results.append(r)

        if checkpoint_json_path:
            write_json_checkpoint(results, checkpoint_json_path)

    return results

# -----------------------------
# CLI
# -----------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--works-cited", default="", help="Path to Works Cited text file (any format).")
    ap.add_argument("--urls", default="", help="Comma-separated URLs (optional).")
    ap.add_argument("--out-md", default="report_v3.md", help="Markdown report path.")
    ap.add_argument("--out-json", default="report_v3.json", help="JSON output path.")
    ap.add_argument("--intended-use", choices=["A","B","C"], default="B")
    ap.add_argument("--relation", choices=["auto","self","adversary","third_party","non_political_fact","unknown"], default="auto")
    ap.add_argument("--mode", choices=["heuristic","llm","hybrid"], default="hybrid")
    ap.add_argument("--llm-model", default="gpt-5.2")
    ap.add_argument("--cache-dir", default=".cache_sources_v3", help="Cache directory.")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_S, help="Seconds to sleep between requests.")
    ap.add_argument("--max-aux-pages", type=int, default=6, help="Max policy/aux pages per source.")
    ap.add_argument("--checkpoint", default="", help="Optional checkpoint JSON path (writes progress after each source).")
    args = ap.parse_args()

    items: List[Tuple[str, str]] = []
    if args.works_cited:
        items = parse_works_cited_lines(args.works_cited)

    if args.urls:
        urls = [normalize_url(u) for u in args.urls.split(",") if u.strip()]
        if not items and urls:
            items = [("", u) for u in urls]

    if not items:
        raise SystemExit("No input found. Provide --works-cited or --urls.")

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
        checkpoint_json_path=checkpoint_path
    )

    write_markdown(results, args.out_md)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(to_json(results), f, ensure_ascii=False, indent=2)

    print(f"Wrote: {args.out_md}")
    print(f"Wrote: {args.out_json}")
    print(f"Checkpoint: {checkpoint_path}")

if __name__ == "__main__":
    main()

