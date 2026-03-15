#!/usr/bin/env python3
"""
Source Evaluator v5 (HSUS 0–100) — BUILD 2026-01-27a

Purpose
- Evaluate the credibility of sources for three intended uses:
  A = official position / narrative ("what they claim")
  B = factual support ("what happened")
  C = analytic context ("background / interpretation")

Core principles (carried forward from v1–v4)
- Evidence-driven: scoring must be grounded in what is retrievable in this run.
- Fetchability != credibility: paywalls / bot-blocks / JS-only pages are "low confidence",
  not auto-junk. They do, however, limit how high the score can responsibly go.
- Separate (1) gating (reject/restrict) from (2) scoring (C1–C10).
- Optional LLM judge is *evidence-bound* and *quote-validated*; if it fails validation,
  the script falls back to heuristics.

Output
- C1..C10 scored 0–2 (total 0–20) -> HSUS 0–100 (x5)
- Recommendation:
    85–100  Preferred
    65–80   Usable with safeguards
    45–60   Context-only
     0–40   Do not use
- Extra high-stakes rule (B): severe harm claims require stronger evidence.
- Severity coding support check: extent / systematicity / institutionalization.

Notes
- This script intentionally avoids "domain reputation lists" for scoring. Instead, it tries to
  infer publisher signals from the site itself (about/standards/corrections pages).
- You *can* maintain a local satire/spam blocklist if needed; by default only a few are included.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Optional: tldextract for registrable domains (better than urlparse netloc)
try:
    import tldextract  # type: ignore
    HAS_TLDEXTRACT = True
except Exception:
    HAS_TLDEXTRACT = False

# Optional: readability for cleaner extraction
try:
    from readability import Document  # type: ignore
    HAS_READABILITY = True
except Exception:
    HAS_READABILITY = False

# Optional: PDF extraction
try:
    from pdfminer.high_level import extract_text as pdf_extract_text  # type: ignore
    HAS_PDFMINER = True
except Exception:
    HAS_PDFMINER = False

# Optional: OpenAI SDK (Responses API)
try:
    from openai import OpenAI  # type: ignore
    HAS_OPENAI = True
except Exception:
    HAS_OPENAI = False

# -----------------------------
# Logging / warnings
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("pdfminer").setLevel(logging.ERROR)

try:
    import warnings
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:
    pass

# -----------------------------
# Constants
# -----------------------------
DEFAULT_TIMEOUT_S = 25
DEFAULT_SLEEP_S = 0.8

USER_AGENT = "SourceEvaluatorBot/5.0 (+contact: research@yourcompany.example)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8,*;q=0.3",
}

CRAWL_PATHS = [
    "/about", "/about-us", "/contact", "/contact-us",
    "/editorial-policy", "/editorial", "/ethics", "/code-of-ethics", "/code-of-conduct",
    "/standards", "/values", "/principles", "/mission", "/governance",
    "/methods", "/methodology",
    "/corrections", "/correction", "/retractions", "/retraction",
    "/fact-check", "/factcheck", "/faq",
    "/terms", "/privacy", "/policies",
]

POLICY_KEYWORDS = [
    "about", "contact", "editorial", "ethic", "code-of", "standard", "values", "principle",
    "mission", "governance", "ownership", "funding",
    "correction", "retraction", "policy", "privacy", "terms",
    "method", "methodology", "factcheck", "fact-check", "faq",
]

SATIRE_KEYWORDS = ["satire", "parody", "humor", "humour", "comedy", "entertainment"]
KNOWN_SATIRE_DOMAINS = {
    "theonion.com", "babylonbee.com", "clickhole.com",
}

# Minimal hard blocklist (optional)
KNOWN_BAD_DOMAINS: set[str] = set()

PAYWALL_HINTS = [
    "subscribe to continue", "subscribe now", "sign in to continue",
    "membership required", "register to continue", "start your subscription",
    "enable cookies", "enable javascript",
]
BOTBLOCK_HINTS = [
    "verify you are human", "captcha", "cloudflare", "unusual traffic",
    "access denied", "request blocked", "bot detection", "ddos protection",
]

HIGH_STAKES_KEYWORDS = [
    "genocide", "ethnic cleansing", "forced labor", "forced labour", "concentration camp",
    "torture", "rape", "sexual violence", "killed", "executed", "extrajudicial",
    "disappeared", "enforced disappearance", "mass detention", "arbitrary detention",
    "organ harvesting", "mass surveillance", "forced sterilization", "child separation",
]

# For "severity coding support" signals
EXTENT_HINTS = ["years", "months", "sentenced", "imprisoned", "detained", "arrested", "killed", "injured", "tortured", "raped"]
SYSTEMATICITY_HINTS = ["systematic", "widespread", "routine", "pattern", "across the country", "nationwide", "regularly", "since", "over years", "dozens", "hundreds", "thousands"]
INSTITUTION_HINTS = ["law", "regulation", "policy", "ministry", "bureau", "agency", "court", "prosecutor", "security services", "state media", "directive", "campaign", "program", "budget"]

# -----------------------------
# Data models
# -----------------------------
@dataclass
class FetchedDoc:
    url: str
    final_url: str = ""
    domain: str = ""
    fetch_status: str = "unknown"  # ok/pdf/http_error/timeout/error/unknown
    status_code: Optional[int] = None
    content_type: str = ""
    bytes_downloaded: int = 0
    fetched_at: str = ""
    html: str = ""
    text: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    updated: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    page_type: str = "unknown"     # article/listing/home/about/policy/pdf/unknown
    completeness: str = "unknown"  # complete/partial/failed
    warnings: List[str] = field(default_factory=list)

@dataclass
class Criterion:
    score: int
    reason: str
    evidence_quotes: List[str] = field(default_factory=list)

@dataclass
class Gate:
    auto_reject: bool = False
    reasons: List[str] = field(default_factory=list)
    auto_restrict: bool = False
    restrict_reason: str = ""

@dataclass
class EvalResult:
    url: str
    final_url: str
    domain: str
    intended_use: str
    relation: str
    fetch_status: str
    content_type: str
    page_type: str
    completeness: str
    confidence: str
    high_stakes_claim_detected: bool
    severity_support: bool
    severity_missing: List[str]
    gating: Gate
    criteria: Dict[str, Criterion]
    total_0_20: int
    hsus_0_100: int
    recommendation: str
    works_cited_entry: str
    evidence_pages: List[str]
    llm_used: bool = False
    llm_error: str = ""
    warnings: List[str] = field(default_factory=list)

# -----------------------------
# Helpers
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def strip_trailing_url_punct(u: str) -> str:
    return u.rstrip(").,;]}>\"'")

def extract_urls(text: str) -> List[str]:
    raw = re.findall(r"https?://[^\s<>\]\)\"']+", text, flags=re.IGNORECASE)
    out: List[str] = []
    seen: set[str] = set()
    for u in raw:
        u2 = strip_trailing_url_punct(u).replace("\\", "")
        if u2 and u2 not in seen:
            seen.add(u2)
            out.append(u2)
    return out

def registrable_domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        netloc = netloc.split("@")[-1].split(":")[0]
        if not netloc:
            return ""
        if HAS_TLDEXTRACT:
            ext = tldextract.extract(netloc)  # type: ignore
            if ext.registered_domain:
                return ext.registered_domain.lower()
        return netloc
    except Exception:
        return ""

def is_probably_gov_domain(domain: str) -> bool:
    d = domain.lower()
    return d.endswith(".gov") or d.endswith(".mil") or d.endswith(".gov.uk") or d.endswith(".gouv.fr")

def normalize_for_match(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    cleaned: List[str] = []
    seen: set[str] = set()
    for ln in lines:
        if not ln or len(ln) <= 2:
            continue
        if re.fullmatch(r"[\W_]+", ln):
            continue
        if ln in seen:
            continue
        seen.add(ln)
        cleaned.append(ln)
    out = "\n".join(cleaned)
    out = re.sub(r"[ \t]+", " ", out).strip()
    return out

def detect_paywall_or_botblock(html_or_text: str) -> Tuple[bool, bool]:
    s = normalize_for_match(html_or_text)
    paywall = any(normalize_for_match(x) in s for x in PAYWALL_HINTS)
    botblock = any(normalize_for_match(x) in s for x in BOTBLOCK_HINTS)
    return paywall, botblock

def looks_like_listing_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(seg in path for seg in ["/section/", "/tag/", "/tags/", "/category/", "/topics/", "/topic/", "/archive", "/archives", "/search", "/index"]) or path.endswith("/")

def looks_like_policy_url(url: str) -> bool:
    u = url.lower()
    return any(k in u for k in POLICY_KEYWORDS)

# -----------------------------
# Fetch + extraction
# -----------------------------
def cache_paths(cache_dir: str, url: str) -> Tuple[str, str]:
    h = sha256_hex(url)
    return os.path.join(cache_dir, f"{h}.json"), os.path.join(cache_dir, f"{h}.txt")

def read_cache(cache_dir: str, url: str, max_age_s: int) -> Optional[FetchedDoc]:
    meta_path, text_path = cache_paths(cache_dir, url)
    if not os.path.exists(meta_path) or not os.path.exists(text_path):
        return None
    try:
        age = time.time() - os.stat(meta_path).st_mtime
        if max_age_s >= 0 and age > max_age_s:
            return None
        meta = json.loads(open(meta_path, "r", encoding="utf-8").read())
        text = open(text_path, "r", encoding="utf-8", errors="ignore").read()
        doc = FetchedDoc(**meta)
        doc.text = text
        return doc
    except Exception:
        return None

def write_cache(cache_dir: str, url: str, doc: FetchedDoc) -> None:
    ensure_dir(cache_dir)
    meta_path, text_path = cache_paths(cache_dir, url)
    try:
        meta = dataclasses.asdict(doc)
        meta["html"] = ""  # don’t cache huge HTML blobs
        meta["text"] = ""
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(doc.text or "")
    except Exception:
        pass

def extract_metadata_and_text_from_html(html: str, url: str) -> Tuple[Dict[str, str], str, str]:
    soup = BeautifulSoup(html, "html.parser")
    meta: Dict[str, str] = {}

    def get_meta(name: str) -> str:
        tag = soup.find("meta", attrs={"name": name})
        return str(tag.get("content")).strip() if tag and tag.get("content") else ""

    def get_prop(prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop})
        return str(tag.get("content")).strip() if tag and tag.get("content") else ""

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = title or get_prop("og:title") or get_meta("twitter:title") or ""

    meta["description"] = get_prop("og:description") or get_meta("description") or ""
    meta["published_time"] = get_prop("article:published_time") or ""
    meta["modified_time"] = get_prop("article:modified_time") or ""
    meta["author"] = get_meta("author") or get_prop("article:author") or ""

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    for tagname in ["nav", "footer", "header", "aside", "form", "button"]:
        for t in soup.find_all(tagname):
            t.decompose()

    kill_kw = [
        "nav", "menu", "breadcrumb",
        "subscribe", "subscription", "paywall", "cookie", "newsletter",
        "related", "recommend", "share", "social", "comment", "promo",
        "advert", "ad-", "banner", "modal",
    ]
    for t in soup.find_all(True):
        ident = " ".join([t.get("id", ""), " ".join(t.get("class", []))]).lower()
        if any(k in ident for k in kill_kw):
            t.decompose()

    # Strategy 1: JSON-LD articleBody
    jsonld_text = ""
    try:
        for sc in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
            raw = (sc.string or sc.get_text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            candidates: List[Dict[str, Any]] = []
            if isinstance(data, dict):
                candidates.append(data)
            elif isinstance(data, list):
                candidates.extend([x for x in data if isinstance(x, dict)])
            for obj in candidates:
                typ = obj.get("@type") or obj.get("type") or ""
                typ_join = " ".join([str(x) for x in typ]) if isinstance(typ, list) else str(typ)
                if any(t in typ_join for t in ["NewsArticle", "Article", "Report"]):
                    body = obj.get("articleBody") or obj.get("text") or ""
                    if isinstance(body, str) and len(body) > len(jsonld_text):
                        jsonld_text = body
                        if not meta.get("published_time") and obj.get("datePublished"):
                            meta["published_time"] = str(obj.get("datePublished"))
                        if not meta.get("modified_time") and obj.get("dateModified"):
                            meta["modified_time"] = str(obj.get("dateModified"))
                        if not meta.get("author") and obj.get("author"):
                            if isinstance(obj["author"], dict) and obj["author"].get("name"):
                                meta["author"] = str(obj["author"]["name"])
                            elif isinstance(obj["author"], str):
                                meta["author"] = obj["author"]
                        if not title and obj.get("headline"):
                            title = str(obj.get("headline"))
    except Exception:
        pass

    # Strategy 2: <article>
    article_text = ""
    try:
        art = soup.find("article")
        if art:
            article_text = art.get_text(separator="\n")
    except Exception:
        article_text = ""

    # Strategy 3: common containers
    container_text = ""
    try:
        candidates = []
        selectors = [
            "main",
            "div[itemprop='articleBody']",
            "div[class*='article']",
            "div[class*='content']",
            "div[class*='story']",
            "div[class*='post']",
            "section[class*='article']",
            "section[class*='content']",
        ]
        for sel in selectors:
            for node in soup.select(sel):
                txt = node.get_text(separator="\n")
                if txt and len(txt) > 500:
                    candidates.append((len(txt), txt))
        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            container_text = candidates[0][1]
    except Exception:
        container_text = ""

    # Strategy 4: readability-lxml
    readable_text = ""
    if HAS_READABILITY:
        try:
            doc = Document(html)  # type: ignore
            summary = doc.summary(html_partial=True)
            s2 = BeautifulSoup(summary, "html.parser")
            readable_text = s2.get_text(separator="\n")
        except Exception:
            readable_text = ""

    main_text = ""
    for candidate in [jsonld_text, article_text, container_text, readable_text]:
        cand = clean_text(candidate)
        if len(cand) > len(main_text):
            main_text = cand
    if len(main_text) < 200:
        main_text = clean_text(soup.get_text(separator="\n"))

    return meta, title, clean_text(main_text)

def classify_page_type(url: str, html: str, text: str) -> str:
    u = url.lower()
    if u.endswith(".pdf"):
        return "pdf"
    if looks_like_listing_url(url) and len(text) < 1200:
        return "listing"
    if looks_like_policy_url(url):
        return "policy"
    if "<article" in html.lower() and len(text) > 800:
        return "article"
    if urlparse(url).path in ("", "/", "/home") and len(text) < 1200:
        return "home"
    return "unknown"

def compute_completeness(fetch_status: str, page_type: str, text: str, html: str) -> str:
    if fetch_status not in ("ok", "pdf"):
        return "failed"
    if page_type in ("listing", "home"):
        return "partial"
    paywall, botblock = detect_paywall_or_botblock(html + "\n" + text)
    if botblock:
        return "partial"
    if paywall and len(text) < 800:
        return "partial"
    if len(text) < 200:
        return "partial"
    return "complete"

def compute_confidence(fetch_status: str, completeness: str, text_len: int) -> str:
    if fetch_status not in ("ok", "pdf") or completeness == "failed":
        return "low"
    if completeness == "partial":
        return "low" if text_len < 400 else "medium"
    return "high" if text_len > 1500 else "medium"

def fetch_doc(
    session: requests.Session,
    url: str,
    cache_dir: str,
    sleep_s: float,
    timeout_s: int,
    cache_max_age_s: int,
    no_cache: bool,
) -> FetchedDoc:
    if not no_cache:
        cached = read_cache(cache_dir, url, cache_max_age_s)
        if cached:
            return cached

    doc = FetchedDoc(url=url, fetched_at=utc_now_iso())
    doc.domain = registrable_domain_from_url(url)

    try:
        resp = session.get(url, headers=HEADERS, timeout=timeout_s, allow_redirects=True)
        doc.status_code = resp.status_code
        doc.final_url = str(resp.url)
        doc.content_type = resp.headers.get("content-type", "")
        doc.bytes_downloaded = len(resp.content or b"")

        if resp.status_code >= 400:
            doc.fetch_status = "http_error"
            doc.warnings.append(f"HTTP {resp.status_code}")
            if not no_cache:
                write_cache(cache_dir, url, doc)
            time.sleep(sleep_s)
            return doc

        # PDF
        if "pdf" in doc.content_type.lower() or doc.final_url.lower().endswith(".pdf"):
            doc.fetch_status = "pdf"
            if HAS_PDFMINER:
                try:
                    ensure_dir(cache_dir)
                    pdf_path = os.path.join(cache_dir, f"{sha256_hex(doc.final_url)}.pdf")
                    with open(pdf_path, "wb") as f:
                        f.write(resp.content)
                    doc.text = clean_text(pdf_extract_text(pdf_path) or "")  # type: ignore
                except Exception as e:
                    doc.warnings.append(f"PDF text extraction failed: {e}")
                    doc.text = ""
            else:
                doc.warnings.append("PDF text extraction unavailable (pdfminer not installed).")
                doc.text = ""
            doc.page_type = "pdf"
            doc.completeness = compute_completeness(doc.fetch_status, doc.page_type, doc.text, "")
            if not no_cache:
                write_cache(cache_dir, url, doc)
            time.sleep(sleep_s)
            return doc

        # HTML/text
        resp.encoding = resp.encoding or "utf-8"
        html = resp.text or ""
        doc.html = html

        # HTML/text extraction (DO NOT let extraction exceptions become fetch failures)
        try:
            meta, title, main_text = extract_metadata_and_text_from_html(html, doc.final_url or url)
            doc.meta = meta or {}
            doc.title = title or ""
            doc.author = (doc.meta.get("author") or "").strip()
            doc.published = (doc.meta.get("published_time") or "").strip()
            doc.updated = (doc.meta.get("modified_time") or "").strip()
            doc.text = main_text or ""
        except Exception as e:
            # Extraction failed; fall back to basic soup text (still counts as fetched)
            doc.warnings.append(f"Extraction error (fallback used): {e}")
            try:
                soup_fallback = BeautifulSoup(html, "html.parser")
                for tag in soup_fallback(["script", "style", "noscript", "svg"]):
                    tag.decompose()
                fallback_text = soup_fallback.get_text(separator="\n")
                doc.text = clean_text(fallback_text)
            except Exception as e2:
                doc.warnings.append(f"Fallback extraction failed: {e2}")
                doc.text = ""

        # Mark as successfully fetched HTML regardless of extraction quality
        doc.fetch_status = "ok"
        doc.page_type = classify_page_type(doc.final_url or url, html, doc.text)
        doc.completeness = compute_completeness(doc.fetch_status, doc.page_type, doc.text, html)

# -----------------------------
# Site crawling (policy pages)
# -----------------------------
def discover_policy_links_from_html(base_url: str, html: str, max_links: int = 12) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    seen: set[str] = set()
    base_dom = registrable_domain_from_url(base_url)

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        u = urljoin(base_url, href).split("#")[0]
        if u in seen:
            continue
        seen.add(u)

        if base_dom and registrable_domain_from_url(u) != base_dom:
            continue

        anchor_txt = (a.get_text(" ", strip=True) or "").lower()
        u_low = u.lower()
        if not (looks_like_policy_url(u_low) or any(k in anchor_txt for k in POLICY_KEYWORDS)):
            continue

        path = urlparse(u).path.lower()
        if any(seg in path for seg in ["/news/", "/article/", "/story/"]) and not looks_like_policy_url(u_low):
            continue

        found.append(u)
        if len(found) >= max_links:
            break
    return found

def crawl_site_pages(
    session: requests.Session,
    main: FetchedDoc,
    cache_dir: str,
    sleep_s: float,
    timeout_s: int,
    cache_max_age_s: int,
    no_cache: bool,
    max_aux_pages: int,
) -> List[FetchedDoc]:
    pages: List[FetchedDoc] = []
    if not main.final_url:
        return pages

    parsed = urlparse(main.final_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    for path in CRAWL_PATHS:
        doc = fetch_doc(session, urljoin(root, path), cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache)
        if doc.fetch_status in ("ok", "pdf") and len(doc.text or "") > 200:
            pages.append(doc)
        if len(pages) >= max_aux_pages:
            break

    if len(pages) < max_aux_pages and (main.html or ""):
        discovered = discover_policy_links_from_html(main.final_url, main.html or "", max_links=14)
        for u in discovered:
            doc = fetch_doc(session, u, cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache)
            if doc.fetch_status in ("ok", "pdf") and len(doc.text or "") > 200:
                pages.append(doc)
            if len(pages) >= max_aux_pages:
                break

    dedup: Dict[str, FetchedDoc] = {}
    for p in pages:
        dedup[p.final_url or p.url] = p
    return list(dedup.values())

# -----------------------------
# Rubric evaluation helpers
# -----------------------------
def infer_relation_auto(url: str, page_type: str, domain: str) -> str:
    if "/about" in url.lower():
        return "self"
    if is_probably_gov_domain(domain):
        return "interested_official"
    return "unknown"

def find_quotes(text: str, patterns: List[str], max_quotes: int = 2, max_len: int = 220) -> List[str]:
    if not text:
        return []
    quotes: List[str] = []
    low = text.lower()
    for pat in patterns:
        idx = low.find(pat.lower())
        if idx == -1:
            continue
        start = max(0, idx - 80)
        end = min(len(text), idx + 140)
        q = re.sub(r"\s+", " ", text[start:end].strip())
        if len(q) > max_len:
            q = q[: max_len - 1].rstrip() + "…"
        if q and q not in quotes:
            quotes.append(q)
        if len(quotes) >= max_quotes:
            break
    return quotes

def detect_high_stakes(text: str) -> bool:
    s = normalize_for_match(text)
    return any(normalize_for_match(k) in s for k in HIGH_STAKES_KEYWORDS)

def severity_coding_support(text: str) -> Tuple[bool, List[str]]:
    low = text.lower()
    missing: List[str] = []
    extent = any(h in low for h in EXTENT_HINTS) or bool(re.search(r"\b\d+\b", low))
    systematicity = any(h in low for h in SYSTEMATICITY_HINTS)
    institutional = any(h in low for h in INSTITUTION_HINTS)

    if not extent:
        missing.append("extent")
    if not systematicity:
        missing.append("systematicity")
    if not institutional:
        missing.append("institutionalization")

    return (extent and systematicity and institutional), missing

def score_heuristic(
    main: FetchedDoc,
    aux_pages: List[FetchedDoc],
    intended_use: str,
    relation: str,
    cross_source_hits: int,
) -> Tuple[Gate, Dict[str, Criterion]]:
    gate = Gate()
    criteria: Dict[str, Criterion] = {}

    domain = main.domain.lower()

    # --- Gating ---
    if domain in KNOWN_BAD_DOMAINS:
        gate.auto_reject = True
        gate.reasons.append("Domain is in local hard blocklist.")
        return gate, {}

    if domain in KNOWN_SATIRE_DOMAINS:
        gate.auto_reject = True
        gate.reasons.append("Satire/parody publisher.")
        return gate, {}

    page_sig = normalize_for_match((main.title or "") + " " + (main.meta.get("description", "") or ""))
    if any(normalize_for_match(x) in page_sig for x in SATIRE_KEYWORDS):
        gate.auto_reject = True
        gate.reasons.append("Satire/parody signals detected in page metadata.")
        return gate, {}

    if intended_use == "B" and relation == "self":
        gate.auto_restrict = True
        gate.restrict_reason = "Self-interest context: treat as narrative/official position, not proof."

    main_text = main.text or ""
    aux_text = "\n\n".join([p.text for p in aux_pages if p.text])[:40000]

    combined = (main_text + "\n\n" + aux_text).strip()

    # --- C1 Ownership/control (default unknown=1) ---
    c1_score = 1
    c1_reason = "Ownership/control unclear from retrieved pages; defaulting to unknown."
    c1_quotes: List[str] = []
    # Prefer aux pages for ownership signals (avoid misreading “independent journalist”)
    if re.search(r"\b(editorial independence|nonprofit|non-profit|board of directors|governance)\b", aux_text, re.I):
        c1_score = 2
        c1_reason = "Publisher presents governance/independence signals on retrieved pages."
        c1_quotes = find_quotes(aux_text, ["editorial independence", "board", "nonprofit", "governance"], max_quotes=2)
    elif re.search(r"\b(state[- ]owned|owned by the government|party[- ]owned|government[- ]run)\b", aux_text, re.I):
        c1_score = 0
        c1_reason = "Retrieved pages suggest state/party ownership or political control."
        c1_quotes = find_quotes(aux_text, ["state-owned", "government-run", "party-owned"], max_quotes=2)
    criteria["C1"] = Criterion(c1_score, c1_reason, c1_quotes)

    # --- C2 Conflict of interest ---
    if relation == "self":
        criteria["C2"] = Criterion(0, "Publisher is speaking about itself / its own position (direct stake).", [])
    elif relation == "interested_official":
        criteria["C2"] = Criterion(1, "Official/government source: treat as potentially interested.", [])
    elif relation == "third_party":
        criteria["C2"] = Criterion(2, "Third-party relationship: no obvious stake inferred.", [])
    else:
        criteria["C2"] = Criterion(1, "Relationship unclear; treating as potentially interested.", [])

    # --- C3 Evidence type ---
    c3_score, c3_reason, c3_quotes = 0, "Mostly assertions/opinion; limited primary evidence signals detected.", []
    if main.fetch_status == "pdf":
        c3_score, c3_reason = 2, "Primary document (PDF) retrieved; treat as primary evidence."
    else:
        if re.search(r"\b(dataset|court document|indictment|judgment|ruling|law|constitution|transcript|documented)\b", main_text, re.I):
            c3_score, c3_reason = 2, "Primary evidence indicators present (documents/data/official records/media)."
            c3_quotes = find_quotes(main_text, ["court", "judgment", "law", "constitution", "dataset", "transcript"], max_quotes=2)
        elif re.search(r"\b(according to|reported|said|told|stated)\b", main_text, re.I):
            c3_score, c3_reason = 1, "Secondary reporting with attribution language detected."
            c3_quotes = find_quotes(main_text, ["according to", "reported", "said"], max_quotes=2)
        if len(main_text) < 250 and c3_score == 0:
            c3_reason = "Too little retrievable text to assess evidence type."
    criteria["C3"] = Criterion(c3_score, c3_reason, c3_quotes)

    # --- C4 Method transparency ---
    c4_score, c4_reason, c4_quotes = 0, "No clear verification/method description detected.", []
    if re.search(r"\b(methodology|we analyzed|we reviewed|data from|verified|verification)\b", main_text, re.I):
        c4_score, c4_reason = 2, "Clear method/verification language present in the item."
        c4_quotes = find_quotes(main_text, ["methodology", "we analyzed", "verified", "data from"], max_quotes=2)
    elif re.search(r"\b(standards|ethics|code of ethics|corrections|accuracy|we correct)\b", aux_text, re.I):
        c4_score, c4_reason = 1, "Site-level standards/corrections pages found; item-level method not explicit."
        c4_quotes = find_quotes(aux_text, ["code of ethics", "standards", "accuracy", "corrections"], max_quotes=2)
    criteria["C4"] = Criterion(c4_score, c4_reason, c4_quotes)

    # --- C5 Specificity & auditability ---
    c5_score, c5_reason, c5_quotes = 0, "Vague/minimal retrievable text; poor auditability.", []
    if main.completeness == "complete" and len(main_text) >= 600:
        n_nums = len(re.findall(r"\b\d{2,}\b", main_text))
        n_dates = len(re.findall(r"\b(19\d\d|20\d\d)\b", main_text))
        if n_nums + n_dates >= 10:
            c5_score, c5_reason = 2, "High specificity: multiple dates/numbers/actors present."
        elif n_nums + n_dates >= 3:
            c5_score, c5_reason = 1, "Some specificity present, but key anchors may be missing."
        else:
            c5_score, c5_reason = 1, "Readable text present but with limited concrete anchors."
        c5_quotes = find_quotes(main_text, ["202", "201", "million", "court", "said"], max_quotes=2)
    elif len(main_text) >= 250:
        c5_score, c5_reason = 1, "Partial text retrieved; auditability limited."
        c5_quotes = find_quotes(main_text, ["said", "reported", "published"], max_quotes=2)
    criteria["C5"] = Criterion(c5_score, c5_reason, c5_quotes)

    # --- C6 Corroboration potential ---
    c6_score, c6_reason, c6_quotes = 0, "No corroboration detected.", []
    attrib_sources = set(m.lower() for m in re.findall(
        r"\b(Reuters|AP|Associated Press|BBC|CNN|Al Jazeera|Amnesty International|Human Rights Watch|Freedom House|United Nations|OHCHR|Xinhua|Global Times)\b",
        main_text, flags=re.I
    ))
    if len(attrib_sources) >= 2:
        c6_score, c6_reason = 1, "Multiple external attributions detected within the item; corroboration potential exists."
        c6_quotes = find_quotes(main_text, ["according to", "reported", "said"], max_quotes=2)
    if cross_source_hits >= 1:
        c6_score = max(c6_score, 1)
        c6_reason = "Similar claim/topic appears in multiple sources in this run (lightweight cross-check)."
    criteria["C6"] = Criterion(c6_score, c6_reason, c6_quotes)

    # --- C7 Legal/institutional confirmation ---
    c7_score, c7_reason, c7_quotes = 0, "No legal/institutional confirmation signals detected.", []
    if re.search(r"\b(court|sentenced|convicted|charged|indicted|ruling|verdict|law|constitution|regulation)\b", main_text, re.I):
        if re.search(r"\b(sentenced|convicted|verdict|judgment|ruling|law|constitution)\b", main_text, re.I):
            c7_score, c7_reason = 2, "Formal legal/institutional action language present."
        else:
            c7_score, c7_reason = 1, "Investigation/filing/institutional process referenced."
        c7_quotes = find_quotes(main_text, ["court", "sentenced", "charged", "law", "constitution"], max_quotes=2)
    criteria["C7"] = Criterion(c7_score, c7_reason, c7_quotes)

    # --- C8 Track record & corrections ---
    c8_score, c8_reason, c8_quotes = 1, "Corrections behavior not clearly evidenced; defaulting to unknown.", []
    if re.search(r"\b(corrections|retractions|we correct|clarification|updated on)\b", aux_text, re.I):
        c8_score, c8_reason = 2, "Corrections/retractions behavior indicated on retrieved pages."
        c8_quotes = find_quotes(aux_text, ["correction", "retraction", "we correct", "clarification"], max_quotes=2)
    criteria["C8"] = Criterion(c8_score, c8_reason, c8_quotes)

    # --- C9 Bias handling & nuance ---
    c9_score, c9_reason, c9_quotes = 0, "Little uncertainty/nuance language detected.", []
    if re.search(r"\b(alleged|reportedly|may|might|according to|unclear|not confirmed|could)\b", main_text, re.I):
        c9_score, c9_reason = 1, "Some hedging/attribution suggests basic uncertainty handling."
        c9_quotes = find_quotes(main_text, ["alleged", "reportedly", "unclear", "not confirmed", "according to"], max_quotes=2)
        if re.search(r"\b(denied|spokesperson|government said|critics say)\b", main_text, re.I):
            c9_score, c9_reason = 2, "Acknowledges competing claims/uncertainty; avoids pure one-sided framing."
            c9_quotes = find_quotes(main_text, ["denied", "spokesperson", "critics say", "government said"], max_quotes=2)
    criteria["C9"] = Criterion(c9_score, c9_reason, c9_quotes)

    # --- C10 Domain competence ---
    c10_score, c10_reason, c10_quotes = 1, "Generalist coverage / unclear domain depth.", []
    if re.search(r"\b(peer-reviewed|journal|methodology|appendix|dataset|statistical)\b", main_text, re.I) or domain.endswith(".edu"):
        c10_score, c10_reason = 2, "Domain-competent indicators present (research/technical depth)."
        c10_quotes = find_quotes(main_text, ["methodology", "dataset", "peer-reviewed", "statistical"], max_quotes=2)
    elif len(main_text) > 1200 and re.search(r"\b(revolutionary|game-changer|breakthrough|shocking|you won't believe)\b", main_text, re.I):
        c10_score, c10_reason = 0, "Sensational/buzzword framing detected; limited substance signals."
        c10_quotes = find_quotes(main_text, ["shocking", "you won't believe", "breakthrough"], max_quotes=2)
    criteria["C10"] = Criterion(c10_score, c10_reason, c10_quotes)

    return gate, criteria

def total_and_hsus(criteria: Dict[str, Criterion]) -> Tuple[int, int]:
    total = sum(int(criteria.get(f"C{i}", Criterion(0, "")).score) for i in range(1, 11))
    total = max(0, min(20, total))
    return total, total * 5

def has_primary_anchor_signal(main: FetchedDoc, criteria: Dict[str, Criterion]) -> bool:
    if main.fetch_status == "pdf":
        return True
    return (criteria.get("C3", Criterion(0, "")).score == 2) or (criteria.get("C7", Criterion(0, "")).score == 2)

def recommendation_from(
    hsus: int,
    confidence: str,
    gate: Gate,
    intended_use: str,
    high_stakes: bool,
    c6_score: int,
    has_primary_anchor: bool,
) -> str:
    if gate.auto_reject:
        return "Do not use (auto-reject)"
    if gate.auto_restrict and intended_use != "A":
        return "Restricted: narrative/official position only"
    if confidence == "low":
        return "Context-only (manual retrieval needed: incomplete access/extraction)" if hsus >= 45 else "Do not use (manual retrieval needed: incomplete access/extraction)"
    if intended_use == "B" and high_stakes:
        if hsus < 85 and not (hsus >= 65 and c6_score >= 1 and has_primary_anchor):
            return "Context-only (high-stakes claim: needs stronger corroboration/primary anchor)"
    if hsus >= 85:
        return "Preferred: primary factual support"
    if hsus >= 65:
        return "Usable with safeguards (corroborate for factual support)"
    if hsus >= 45:
        return "Context-only (not for key factual claims)"
    return "Do not use (except possibly as narrative if relevant)"

# -----------------------------
# LLM scoring (evidence-bound)
# -----------------------------
def build_evidence_pack(main: FetchedDoc, aux_pages: List[FetchedDoc], max_main_chars: int = 20000, max_aux_chars_each: int = 7000) -> str:
    main_text = (main.text or "")[:max_main_chars]
    parts = []
    parts.append("EVIDENCE PACK (use ONLY this; do not use outside knowledge)\n")
    parts.append(
        f"MAIN_URL: {main.url}\nFINAL_URL: {main.final_url}\nDOMAIN: {main.domain}\nFETCH_STATUS: {main.fetch_status}\n"
        f"CONTENT_TYPE: {main.content_type}\nPAGE_TYPE: {main.page_type}\nCOMPLETENESS: {main.completeness}\n"
    )
    if main.title:
        parts.append(f"TITLE: {main.title}\n")
    if main.author:
        parts.append(f"AUTHOR_META: {main.author}\n")
    if main.published:
        parts.append(f"PUBLISHED_META: {main.published}\n")
    if main.updated:
        parts.append(f"UPDATED_META: {main.updated}\n")
    if main.meta.get("description"):
        parts.append(f"DESCRIPTION_META: {main.meta.get('description')}\n")

    parts.append("\n=== MAIN_TEXT_START ===\n")
    parts.append(main_text)
    parts.append("\n=== MAIN_TEXT_END ===\n")

    if aux_pages:
        parts.append("\n=== AUX_PAGES_START ===\n")
        for i, p in enumerate(aux_pages, start=1):
            txt = (p.text or "")[:max_aux_chars_each]
            if not txt:
                continue
            parts.append(f"\n[AUX {i}] {p.final_url or p.url}\n")
            parts.append(txt)
            parts.append("\n")
        parts.append("=== AUX_PAGES_END ===\n")
    return "\n".join(parts)

def llm_prompt(intended_use: str, relation: str) -> str:
    return f"""
You are an evidence-bound source evaluator.

You MUST follow these rules:
- Use ONLY the evidence pack provided. Do not use outside knowledge (including domain reputation).
- Ignore site navigation/menu boilerplate if it appears in the evidence pack; focus on meaningful content.
- Score the source for intended use = {intended_use} and relationship = {relation}.
- Output MUST be a single JSON object (no markdown, no extra text).
- Provide evidence quotes that are verbatim substrings from the evidence pack.
- If there is insufficient evidence for a criterion, say so explicitly in the reason.

Rubric (0–2 each, C1..C10):
C1 Ownership/control
C2 Conflict-of-interest vs claim (relation-aware)
C3 Evidence type strength
C4 Method transparency
C5 Specificity & auditability
C6 Corroboration potential
C7 Legal/institutional confirmation (when applicable)
C8 Track record & corrections behavior
C9 Bias handling & nuance
C10 Domain competence

Gating:
- auto_reject: satire/parody, spam/synthetic with no accountability, or clearly unretrievable origin.
- auto_restrict: if relation=self AND intended_use != A, restrict to narrative/official position only.

Return JSON in one of these equivalent shapes:
Option A:
{{
  "auto_reject": false,
  "auto_reject_reasons": [],
  "auto_restrict": false,
  "auto_restrict_reason": "",
  "criteria": {{
    "C1": {{"score": 1, "reason": "...", "evidence_quotes": ["..."]}},
    ...
    "C10": {{"score": 1, "reason": "...", "evidence_quotes": ["..."]}}
  }}
}}

Option B (no nested criteria dict):
{{
  "auto_reject": false,
  "auto_reject_reasons": [],
  "auto_restrict": false,
  "auto_restrict_reason": "",
  "C1": {{"score": 1, "reason": "...", "evidence_quotes": ["..."]}},
  ...
  "C10": {{"score": 1, "reason": "...", "evidence_quotes": ["..."]}}
}}
""".strip()

def validate_llm_payload(payload: Dict[str, Any], evidence_pack: str) -> Tuple[Optional[Gate], Optional[Dict[str, Criterion]], str]:
    if not isinstance(payload, dict):
        return None, None, "LLM payload is not a dict."

    gate = Gate(
        auto_reject=bool(payload.get("auto_reject", False)),
        reasons=[str(x) for x in (payload.get("auto_reject_reasons", []) or payload.get("reasons", [])) if str(x).strip()],
        auto_restrict=bool(payload.get("auto_restrict", False)),
        restrict_reason=str(payload.get("auto_restrict_reason", "") or payload.get("restrict_reason", "")).strip(),
    )

    crit_obj = payload.get("criteria")
    if crit_obj is None:
        crit_obj = {k: payload.get(k) for k in [f"C{i}" for i in range(1, 11)] if k in payload}

    if not isinstance(crit_obj, dict) or any(k not in crit_obj for k in [f"C{i}" for i in range(1, 11)]):
        return gate, None, "LLM payload missing complete C1..C10 criteria dict."

    norm_ev = normalize_for_match(evidence_pack)

    def must_have_quote(ck: str) -> bool:
        return ck not in ("C1", "C2", "C8")

    criteria: Dict[str, Criterion] = {}
    for ck in [f"C{i}" for i in range(1, 11)]:
        obj = crit_obj.get(ck)
        if not isinstance(obj, dict):
            return gate, None, f"{ck} is not an object."
        score = obj.get("score")
        if not isinstance(score, int) or score not in (0, 1, 2):
            return gate, None, f"{ck}.score invalid (must be 0/1/2)."
        reason = str(obj.get("reason", "")).strip()
        quotes = obj.get("evidence_quotes", obj.get("quotes", [])) or []
        if not isinstance(quotes, list):
            return gate, None, f"{ck}.evidence_quotes must be a list."
        quotes = [str(q) for q in quotes if str(q).strip()]

        if must_have_quote(ck) and score > 0 and len(quotes) == 0 and "insufficient evidence" not in reason.lower():
            return gate, None, f"{ck}: missing quotes; reason must say 'insufficient evidence'."

        for q in quotes:
            nq = normalize_for_match(q)
            if len(nq) < 12:
                return gate, None, f"{ck}: quote too short."
            if nq not in norm_ev:
                return gate, None, f"{ck}: evidence quote not found in evidence pack (must be verbatim)."

        criteria[ck] = Criterion(score=score, reason=reason, evidence_quotes=quotes)

    return gate, criteria, ""

def llm_score(evidence_pack: str, intended_use: str, relation: str, model: str, timeout_s: int = 60) -> Tuple[Optional[Gate], Optional[Dict[str, Criterion]], str]:
    if not HAS_OPENAI:
        return None, None, "OpenAI SDK not installed."
    if not os.environ.get("OPENAI_API_KEY"):
        return None, None, "OPENAI_API_KEY not set."

    try:
        try:
            client = OpenAI(timeout=timeout_s)  # type: ignore
        except Exception:
            client = OpenAI()  # type: ignore

        prompt = llm_prompt(intended_use, relation)

        resp = client.responses.create(  # type: ignore
            model=model,
            input=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": evidence_pack},
            ],
            text={"format": {"type": "json_object"}},
        )

        raw_text = getattr(resp, "output_text", "") or ""
        raw_text = raw_text.strip()
        if not raw_text:
            return None, None, "LLM returned empty output."

        try:
            data = json.loads(raw_text)
        except Exception as e:
            m = re.search(r"\{.*\}", raw_text, flags=re.S)
            if not m:
                return None, None, f"LLM returned non-JSON: {e}"
            data = json.loads(m.group(0))

        gate, crit, err = validate_llm_payload(data, evidence_pack)
        if err:
            return None, None, err
        return gate, crit, ""

    except Exception as e:
        return None, None, f"LLM error: {e}"

# -----------------------------
# Reporting
# -----------------------------
def render_report_md(results: List[EvalResult], build: str) -> str:
    out: List[str] = []
    out.append("# Source Evaluation Report\n")
    out.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d')}_\n")
    out.append(f"_Build: {build}_\n")

    for r in results:
        out.append(f"## {r.final_url or r.url}\n")
        out.append(f"- **Domain:** {r.domain}\n")
        out.append(f"- **Intended use:** {r.intended_use}\n")
        out.append(f"- **Relation:** {r.relation}\n")
        out.append(f"- **Fetch status:** {r.fetch_status} ({r.content_type})\n")
        out.append(f"- **Page type:** {r.page_type}\n")
        out.append(f"- **Completeness:** {r.completeness}\n")
        out.append(f"- **Confidence:** {r.confidence}\n")
        out.append(f"- **High-stakes claim detected:** {str(r.high_stakes_claim_detected)}\n")
        out.append(f"- **Severity coding support:** {str(r.severity_support)}\n")
        if r.severity_missing:
            out.append(f"- **Severity missing:** {', '.join(r.severity_missing)}\n")
        out.append(f"- **HSUS (0–100):** {r.hsus_0_100}\n")
        out.append(f"- **Total (0–20):** {r.total_0_20}\n")
        out.append(f"- **Recommendation:** {r.recommendation}\n")
        out.append(f"- **LLM used:** {str(r.llm_used)}\n")
        if r.llm_error:
            out.append(f"- **LLM error:** {r.llm_error}\n")

        if r.gating.auto_reject or r.gating.auto_restrict:
            out.append("\n### Gating\n")
            out.append(f"- **auto_reject:** {str(r.gating.auto_reject)}\n")
            if r.gating.reasons:
                out.append(f"- **reasons:** {', '.join(r.gating.reasons)}\n")
            out.append(f"- **auto_restrict:** {str(r.gating.auto_restrict)}\n")
            if r.gating.restrict_reason:
                out.append(f"- **restrict_reason:** {r.gating.restrict_reason}\n")

        if r.warnings:
            out.append("\n### Warnings\n")
            for w in r.warnings:
                out.append(f"- {w}\n")

        out.append("\n### Criteria breakdown (0–2 each)\n")
        for ck in [f"C{i}" for i in range(1, 11)]:
            c = r.criteria.get(ck)
            if not c:
                continue
            out.append(f"- **{ck}: {c.score}** — {c.reason}\n")
            for q in (c.evidence_quotes or [])[:2]:
                out.append(f"  - Evidence: “{q}”\n")

        out.append("\n### Evidence pages fetched\n")
        for u in r.evidence_pages:
            out.append(f"- {u}\n")
        out.append("\n---\n")

    out.append("\n# Works Cited\n\n")
    for r in results:
        out.append(f"- {r.works_cited_entry or (r.final_url or r.url)}\n")
    return "\n".join(out)

# -----------------------------
# Evaluation loop
# -----------------------------
def build_cross_source_index(docs: List[FetchedDoc]) -> Dict[str, int]:
    token_sets: List[Tuple[str, set[str]]] = []
    for d in docs:
        toks = set(re.findall(r"[a-z0-9]{4,}", (d.title or "").lower()))
        token_sets.append((d.url, toks))

    hits: Dict[str, int] = {d.url: 0 for d in docs}
    for i in range(len(token_sets)):
        ui, ti = token_sets[i]
        if not ti:
            continue
        for j in range(i + 1, len(token_sets)):
            uj, tj = token_sets[j]
            if not tj:
                continue
            if len(ti.intersection(tj)) >= 6:
                hits[ui] += 1
                hits[uj] += 1
    return hits

def evaluate_sources(
    urls: List[str],
    intended_use: str,
    relation: str,
    mode: str,
    llm_model: str,
    max_aux_pages: int,
    cache_dir: str,
    cache_max_age_s: int,
    no_cache: bool,
    sleep_s: float,
    timeout_s: int,
    checkpoint_path: str,
) -> List[EvalResult]:
    session = requests.Session()

    # Dedup URLs
    all_urls: List[str] = []
    seen: set[str] = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            all_urls.append(u)

    fetched: List[FetchedDoc] = []
    aux_map: Dict[str, List[FetchedDoc]] = {}

    for i, u in enumerate(all_urls, start=1):
        d = fetch_doc(session, u, cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache)
        aux_pages: List[FetchedDoc] = []
        if d.fetch_status in ("ok", "pdf") and max_aux_pages > 0:
            aux_pages = crawl_site_pages(session, d, cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache, max_aux_pages)
        fetched.append(d)
        aux_map[d.url] = aux_pages
        print(f"[{i}/{len(all_urls)}] {u} status={d.fetch_status} page_type={d.page_type} completeness={d.completeness} text_len={len(d.text or '')}")

    cross_hits = build_cross_source_index(fetched)

    results: List[EvalResult] = []
    checkpoint_rows: List[Dict[str, Any]] = []

    for d in fetched:
        aux_pages = aux_map.get(d.url, [])
        rel = infer_relation_auto(d.final_url or d.url, d.page_type, d.domain) if relation == "auto" else relation
        confidence = compute_confidence(d.fetch_status, d.completeness, len(d.text or ""))

        high_stakes = detect_high_stakes(d.text or "")
        sev_support, sev_missing = severity_coding_support(d.text or "")

        gate_h, crit_h = score_heuristic(d, aux_pages, intended_use, rel, cross_hits.get(d.url, 0))

        gate, criteria = gate_h, crit_h
        llm_used, llm_error = False, ""

        def should_call_llm() -> bool:
            if mode not in ("llm", "hybrid"):
                return False
            if d.fetch_status not in ("ok", "pdf"):
                return False
            if gate_h.auto_reject:
                return False
            if len(d.text or "") < 450 and d.fetch_status != "pdf":
                return False
            if mode == "hybrid" and confidence == "low":
                return False
            return True

        if should_call_llm():
            ep = build_evidence_pack(d, aux_pages)
            gate_llm, crit_llm, err = llm_score(ep, intended_use, rel, llm_model)
            if err:
                llm_error = err
            else:
                gate = gate_llm or gate_h
                criteria = crit_llm or crit_h
                llm_used = True

        total, hsus = total_and_hsus(criteria)
        rec = recommendation_from(
            hsus=hsus,
            confidence=confidence,
            gate=gate,
            intended_use=intended_use,
            high_stakes=high_stakes,
            c6_score=criteria.get("C6", Criterion(0, "")).score,
            has_primary_anchor=has_primary_anchor_signal(d, criteria),
        )

        warnings_all = list(d.warnings)
        if llm_error and mode in ("llm", "hybrid"):
            warnings_all.append(f"LLM failed/invalid; using heuristics. ({llm_error})")

        ev_pages = [d.final_url or d.url] + [(p.final_url or p.url) for p in aux_pages]

        res = EvalResult(
            url=d.url,
            final_url=d.final_url or d.url,
            domain=d.domain,
            intended_use=intended_use,
            relation=rel,
            fetch_status=d.fetch_status,
            content_type=d.content_type,
            page_type=d.page_type,
            completeness=d.completeness,
            confidence=confidence,
            high_stakes_claim_detected=high_stakes,
            severity_support=sev_support,
            severity_missing=sev_missing,
            gating=gate,
            criteria=criteria,
            total_0_20=total,
            hsus_0_100=hsus,
            recommendation=rec,
            works_cited_entry=d.final_url or d.url,
            evidence_pages=ev_pages,
            llm_used=llm_used,
            llm_error=llm_error,
            warnings=warnings_all,
        )
        results.append(res)

        checkpoint_rows.append(dataclasses.asdict(res))
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(checkpoint_rows, f, ensure_ascii=False, indent=2)

    return results

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Source Evaluator v5 (HSUS 0–100)")
    p.add_argument("--works-cited", default="", help="Path to works cited text file (any format).")
    p.add_argument("--urls", default="", help="Comma-separated URL(s) to evaluate.")
    p.add_argument("--intended-use", required=True, choices=["A", "B", "C"])
    p.add_argument("--relation", default="auto", choices=["auto", "unknown", "self", "third_party", "interested_official"])
    p.add_argument("--mode", default="hybrid", choices=["heuristic", "llm", "hybrid"])
    p.add_argument("--llm-model", default="gpt-5.2")
    p.add_argument("--max-aux-pages", type=int, default=3)
    p.add_argument("--cache-dir", default=".cache_source_eval")
    p.add_argument("--cache-max-age-s", type=int, default=7 * 24 * 3600)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--sleep-s", type=float, default=DEFAULT_SLEEP_S)
    p.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--out-md", default="report.md")
    p.add_argument("--out-json", default="report.json")
    p.add_argument("--checkpoint", default="report.partial.json")
    return p.parse_args(argv)

def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    urls: List[str] = []
    if args.works_cited:
        works_text = open(args.works_cited, "r", encoding="utf-8", errors="ignore").read()
        urls.extend(extract_urls(works_text))
    if args.urls:
        urls.extend([u.strip() for u in args.urls.split(",") if u.strip().startswith("http")])

    if not urls:
        print("No URLs found. Provide --works-cited and/or --urls.")
        sys.exit(2)

    results = evaluate_sources(
        urls=urls,
        intended_use=args.intended_use,
        relation=args.relation,
        mode=args.mode,
        llm_model=args.llm_model,
        max_aux_pages=args.max_aux_pages,
        cache_dir=args.cache_dir,
        cache_max_age_s=args.cache_max_age_s,
        no_cache=args.no_cache,
        sleep_s=args.sleep_s,
        timeout_s=args.timeout_s,
        checkpoint_path=args.checkpoint,
    )

    build = "Source Evaluator v5 — 2026-01-27a"
    md = render_report_md(results, build)

    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write(md)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(r) for r in results], f, ensure_ascii=False, indent=2)

    print(f"Wrote: {args.out_md}")
    print(f"Wrote: {args.out_json}")
    print(f"Checkpoint: {args.checkpoint}")

if __name__ == "__main__":
    main()

