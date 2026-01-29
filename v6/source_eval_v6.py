#!/usr/bin/env python3
"""
Source Evaluator v6 — HRF Source Credibility Standard (Practical v1)

Purpose: Evaluate sources for HRF's three intended uses:
  A = narrative (what an actor claims)
  B = factual support (what can be verified happened)
  C = analysis/context (interpretation)

Key principles:
- Use-permission labels, not numeric scores
- "Not assessed" is valid when evidence is missing
- Access failure != credibility failure
- LLM enforces use constraints, doesn't decide truth
- Every decision points to retrieved evidence

Output labels:
- B: Preferred evidence
- B: Usable with safeguards
- C: Context-only
- A: Narrative-only
- Manual retrieval needed
- Do not use
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
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Optional imports
try:
    import tldextract
    HAS_TLDEXTRACT = True
except ImportError:
    HAS_TLDEXTRACT = False

try:
    from readability import Document
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False

try:
    from pdfminer.high_level import extract_text as pdf_extract_text
    HAS_PDFMINER = True
except ImportError:
    HAS_PDFMINER = False

try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

# Logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("pdfminer").setLevel(logging.ERROR)

try:
    import warnings
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:
    pass

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
DEFAULT_TIMEOUT_S = 25
DEFAULT_SLEEP_S = 0.8

USER_AGENT = "HRFSourceEvaluator/6.0 (+research)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}

# Paths to crawl for publisher signals
CRAWL_PATHS = [
    "/about", "/about-us", "/contact",
    "/editorial-policy", "/editorial", "/ethics", "/code-of-ethics",
    "/standards", "/methodology", "/methods",
    "/corrections", "/retractions",
    "/terms", "/privacy", "/governance", "/ownership",
]

POLICY_KEYWORDS = [
    "about", "editorial", "ethic", "standard", "correction", "retraction",
    "methodology", "governance", "ownership", "funding", "transparency",
]

# Auto-reject domains
KNOWN_SATIRE_DOMAINS = {"theonion.com", "babylonbee.com", "clickhole.com"}
SATIRE_KEYWORDS = ["satire", "parody", "humor", "humour", "comedy site"]

# Paywall/bot detection
PAYWALL_HINTS = [
    "subscribe to continue", "subscribe now", "sign in to continue",
    "membership required", "register to continue", "start your subscription",
]
BOTBLOCK_HINTS = [
    "verify you are human", "captcha", "cloudflare", "unusual traffic",
    "access denied", "request blocked", "bot detection",
]

# Primary anchor keywords (for evidence strength)
PRIMARY_ANCHOR_KEYWORDS = [
    "law", "regulation", "constitution", "court", "judgment", "ruling",
    "verdict", "sentenced", "convicted", "indictment", "filing",
    "official record", "transcript", "dataset", "document", "decree",
    "resolution", "statute", "ordinance",
]

# Severity coding (for systematic/widespread claims)
SYSTEMATIC_CLAIM_KEYWORDS = [
    "systematic", "widespread", "state policy", "government policy",
    "nationwide", "across the country", "mass", "genocide", "ethnic cleansing",
    "crimes against humanity",
]

EXTENT_HINTS = [
    "killed", "died", "detained", "arrested", "imprisoned", "tortured",
    "sentenced", "disappeared", "injured", "displaced",
]
SYSTEMATICITY_HINTS = [
    "systematic", "widespread", "routine", "pattern", "regular", "ongoing",
    "since", "over years", "months", "decades", "hundreds", "thousands",
]
INSTITUTIONALIZATION_HINTS = [
    "law", "regulation", "policy", "ministry", "bureau", "agency", "court",
    "security services", "state media", "directive", "campaign", "program",
    "official", "government", "party", "state",
]


# -----------------------------------------------------------------------------
# Enums and Data Models
# -----------------------------------------------------------------------------
class IntendedUse(str, Enum):
    A = "A"  # Narrative
    B = "B"  # Factual support
    C = "C"  # Analysis/context


class RelationshipType(str, Enum):
    SELF_INTEREST = "self_interest"      # Speaking about themselves
    OFFICIAL_STATE = "official_state"    # Government/state source
    THIRD_PARTY = "third_party"          # Independent third party
    UNKNOWN = "unknown"


class Completeness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class EvidenceStrength(str, Enum):
    STRONG = "strong"    # Primary anchors
    MEDIUM = "medium"    # Secondary with attribution
    WEAK = "weak"        # Assertions/opinion
    NOT_ASSESSED = "not_assessed"


class CorroborationStatus(str, Enum):
    CORROBORATED = "corroborated"
    NOT_CORROBORATED = "not_corroborated"
    NOT_ASSESSED = "not_assessed"  # Single source run


class SeveritySupport(str, Enum):
    SUPPORTED = "supported"          # All three present
    PARTIAL = "partial"              # Some present
    NOT_APPLICABLE = "not_applicable"  # Claim isn't systematic
    NOT_ASSESSED = "not_assessed"


class UsePermission(str, Enum):
    B_PREFERRED = "B: Preferred evidence"
    B_SAFEGUARDS = "B: Usable with safeguards"
    C_CONTEXT = "C: Context-only"
    A_NARRATIVE = "A: Narrative-only"
    MANUAL_RETRIEVAL = "Manual retrieval needed"
    DO_NOT_USE = "Do not use"


@dataclass
class FetchedDoc:
    url: str
    final_url: str = ""
    domain: str = ""
    fetch_status: str = "unknown"  # ok/pdf/http_error/timeout/error
    status_code: Optional[int] = None
    content_type: str = ""
    bytes_downloaded: int = 0
    fetched_at: str = ""
    html: str = ""
    text: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


@dataclass
class Check:
    """A single check result with evidence."""
    status: str  # The assessment result
    reason: str  # Explanation
    evidence_quotes: List[str] = field(default_factory=list)
    assessed: bool = True  # False if "Not assessed"


@dataclass
class PublisherSignals:
    """Optional publisher-level signals (Part 2 of HRF rubric)."""
    ownership_transparency: Check = field(default_factory=lambda: Check(
        status="not_assessed", reason="Not found in retrieved pages", assessed=False
    ))
    corrections_behavior: Check = field(default_factory=lambda: Check(
        status="not_assessed", reason="Not found in retrieved pages", assessed=False
    ))
    standards_transparency: Check = field(default_factory=lambda: Check(
        status="not_assessed", reason="Not found in retrieved pages", assessed=False
    ))


@dataclass
class CoreChecks:
    """Core checks (Part 1 of HRF rubric) - always required."""
    # 1) Intended use
    intended_use: IntendedUse = IntendedUse.C
    use_constraints: str = ""

    # 2) Relationship / self-interest
    relationship: RelationshipType = RelationshipType.UNKNOWN
    a_only_restriction: bool = False
    relationship_reason: str = ""

    # 3) Access & completeness
    completeness: Completeness = Completeness.FAILED
    completeness_reason: str = ""

    # 4) Evidence strength
    evidence_strength: EvidenceStrength = EvidenceStrength.NOT_ASSESSED
    evidence_reason: str = ""
    evidence_quotes: List[str] = field(default_factory=list)

    # 5) Specificity & auditability
    has_specificity: bool = False
    specificity_reason: str = ""
    specificity_anchors: List[str] = field(default_factory=list)  # who/what/when/where/how much

    # 6) Corroboration
    corroboration: CorroborationStatus = CorroborationStatus.NOT_ASSESSED
    corroboration_reason: str = ""

    # 7) Severity support (only for systematic claims)
    severity_claim_detected: bool = False
    severity_support: SeveritySupport = SeveritySupport.NOT_APPLICABLE
    severity_reason: str = ""
    severity_missing: List[str] = field(default_factory=list)


@dataclass
class EvalResult:
    """Complete evaluation result."""
    url: str
    final_url: str
    domain: str

    # Final determination
    use_permission: UsePermission = UsePermission.DO_NOT_USE
    permission_reason: str = ""

    # Core checks
    core: CoreChecks = field(default_factory=CoreChecks)

    # Publisher signals
    publisher: PublisherSignals = field(default_factory=PublisherSignals)

    # Metadata
    fetch_status: str = ""
    content_type: str = ""
    text_length: int = 0
    evidence_pages: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # LLM tracking
    llm_used: bool = False
    llm_error: str = ""
    llm_decisions: List[str] = field(default_factory=list)  # Which checks used LLM


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def normalize(s: str) -> str:
    """Normalize text for matching."""
    if not s:
        return ""
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()


def clean_text(text: str) -> str:
    """Clean extracted text."""
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    cleaned = []
    seen = set()
    for ln in lines:
        if not ln or len(ln) <= 2:
            continue
        if re.fullmatch(r"[\W_]+", ln):
            continue
        if ln in seen:
            continue
        seen.add(ln)
        cleaned.append(ln)
    return "\n".join(cleaned)


def extract_urls(text: str) -> List[str]:
    """Extract URLs from text."""
    raw = re.findall(r"https?://[^\s<>\]\)\"']+", text, flags=re.IGNORECASE)
    out = []
    seen = set()
    for u in raw:
        u = u.rstrip(").,;]}>\"'").replace("\\", "")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def registrable_domain(url: str) -> str:
    """Get registrable domain from URL."""
    try:
        netloc = urlparse(url).netloc.lower()
        netloc = netloc.split("@")[-1].split(":")[0]
        if not netloc:
            return ""
        if HAS_TLDEXTRACT:
            ext = tldextract.extract(netloc)
            # Use top_domain_under_public_suffix if available (newer API), else registered_domain
            domain = getattr(ext, 'top_domain_under_public_suffix', None) or getattr(ext, 'registered_domain', None)
            if domain:
                return domain.lower()
        return netloc
    except Exception:
        return ""


def find_quotes(text: str, keywords: List[str], max_quotes: int = 2, context_chars: int = 100) -> List[str]:
    """Find evidence quotes around keywords."""
    if not text:
        return []
    quotes = []
    low = text.lower()
    for kw in keywords:
        idx = low.find(kw.lower())
        if idx == -1:
            continue
        start = max(0, idx - context_chars)
        end = min(len(text), idx + len(kw) + context_chars)
        q = re.sub(r"\s+", " ", text[start:end].strip())
        if len(q) > 250:
            q = q[:247] + "..."
        if q and q not in quotes:
            quotes.append(q)
        if len(quotes) >= max_quotes:
            break
    return quotes


# -----------------------------------------------------------------------------
# LLM Augmentation
# -----------------------------------------------------------------------------
DEFAULT_LLM_MODEL = "claude-3-haiku-20240307"
LLM_MAX_TEXT_CHARS = 4000  # Truncate text sent to LLM


def get_anthropic_client() -> Optional[Any]:
    """Get Anthropic client if available and API key is set."""
    if not HAS_ANTHROPIC:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        return Anthropic(api_key=api_key)
    except Exception:
        return None


def llm_review(client: Any, prompt: str, model: str = DEFAULT_LLM_MODEL) -> Optional[Dict[str, Any]]:
    """Call Claude API for review. Returns parsed JSON or None on error."""
    if not client:
        return None
    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        # Extract JSON from response (handle markdown code blocks)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        logging.debug(f"LLM review error: {e}")
        return None


def llm_assess_evidence_strength(
    client: Any,
    text: str,
    heuristic_strength: str,
    model: str = DEFAULT_LLM_MODEL
) -> Optional[Tuple[str, str]]:
    """LLM review of evidence strength. Returns (strength, reason) or None."""
    truncated = text[:LLM_MAX_TEXT_CHARS] if len(text) > LLM_MAX_TEXT_CHARS else text
    prompt = f"""Analyze this source text for evidence strength.

TEXT:
{truncated}

HEURISTIC ASSESSMENT: {heuristic_strength}

Evaluate whether this text contains:
1. Primary evidence (legal documents, court records, official filings, datasets)?
2. Clear attribution to named sources ("according to X", direct quotes)?
3. Verifiable claims with specific details (dates, names, quantities)?

Respond ONLY with JSON:
{{"strength": "strong|medium|weak", "reason": "brief 10-word explanation"}}"""

    result = llm_review(client, prompt, model)
    if result and "strength" in result:
        return result["strength"], result.get("reason", "LLM assessment")
    return None


def llm_assess_self_interest(
    client: Any,
    url: str,
    text: str,
    model: str = DEFAULT_LLM_MODEL
) -> Optional[Tuple[bool, str]]:
    """LLM review for self-interest. Returns (is_self_interest, reason) or None."""
    truncated = text[:LLM_MAX_TEXT_CHARS] if len(text) > LLM_MAX_TEXT_CHARS else text
    prompt = f"""Determine if this source has a self-interest conflict for evidentiary purposes.

URL: {url}

TEXT EXCERPT:
{truncated}

IMPORTANT DISTINCTIONS:
- Self-interest means the source is making claims ABOUT ITSELF or its own organization
- An NGO (Freedom House, Amnesty, HRW) reporting on a GOVERNMENT is NOT self-interest — they are third-party reporters
- A tech company (Google, Microsoft) reporting on EXTERNAL threat actors is NOT self-interest
- A government source making claims about its own policies IS self-interest
- An "about us" page describing the organization IS self-interest
- Research organizations publishing analysis of external actors are NOT self-interest

Is this source primarily making claims about ITSELF (its own organization, its own accomplishments, its own mission)?

Respond ONLY with JSON:
{{"is_self_interest": true|false, "reason": "brief 10-word explanation"}}"""

    result = llm_review(client, prompt, model)
    if result and "is_self_interest" in result:
        return result["is_self_interest"], result.get("reason", "LLM assessment")
    return None


def llm_assess_satire(
    client: Any,
    title: str,
    text: str,
    domain: str = "",
    model: str = DEFAULT_LLM_MODEL
) -> Optional[Tuple[bool, str]]:
    """LLM review for satire/parody. Returns (is_satire, reason) or None."""
    truncated = text[:2000] if len(text) > 2000 else text
    prompt = f"""Determine if this content IS ITSELF satire/parody (not just discussing satire).

DOMAIN: {domain}
TITLE: {title}

TEXT EXCERPT:
{truncated}

CRITICAL DISTINCTIONS:
- A news article ABOUT The Onion or discussing satire is NOT satire - it's journalism
- A BBC/NPR/CNN article covering a satirical story is NOT satire - it's news coverage
- An investigative article from Mother Jones, Wired, or similar is NOT satire - it's journalism
- Only mark as satire if the SOURCE ITSELF is producing satirical/parodic content
- The Onion, Babylon Bee, Clickhole are satire sites
- News organizations reporting ON satirical content are NOT satire

Is this content ITSELF satirical/parodic (the source is producing humor, not reporting on it)?

Respond ONLY with JSON:
{{"is_satire": true|false, "reason": "brief 10-word explanation"}}"""

    result = llm_review(client, prompt, model)
    if result and "is_satire" in result:
        return result["is_satire"], result.get("reason", "LLM assessment")
    return None


def llm_assess_severity_support(
    client: Any,
    text: str,
    missing_elements: List[str],
    model: str = DEFAULT_LLM_MODEL
) -> Optional[Tuple[str, str, List[str]]]:
    """LLM review for severity claim support. Returns (status, reason, still_missing) or None."""
    truncated = text[:LLM_MAX_TEXT_CHARS] if len(text) > LLM_MAX_TEXT_CHARS else text
    prompt = f"""Analyze if this text supports claims of systematic/widespread human rights violations.

TEXT:
{truncated}

HEURISTICS FOUND MISSING: {', '.join(missing_elements)}

For systematic abuse claims, check for evidence of:
1. EXTENT: Scale/severity of harm (deaths, detentions, injuries, numbers affected)
2. SYSTEMATICITY: Pattern/frequency (ongoing, routine, widespread, over time)
3. INSTITUTIONALIZATION: State apparatus involvement (laws, policies, agencies, officials)

Respond ONLY with JSON:
{{"status": "supported|partial|not_supported", "reason": "brief explanation", "still_missing": ["list", "of", "missing"]}}"""

    result = llm_review(client, prompt, model)
    if result and "status" in result:
        return (
            result["status"],
            result.get("reason", "LLM assessment"),
            result.get("still_missing", [])
        )
    return None


def llm_final_review(
    client: Any,
    text: str,
    heuristic_permission: str,
    core_checks_summary: str,
    model: str = DEFAULT_LLM_MODEL
) -> Optional[Tuple[str, str]]:
    """LLM final review for C: Context-only results. Returns (permission, reason) or None."""
    truncated = text[:LLM_MAX_TEXT_CHARS] if len(text) > LLM_MAX_TEXT_CHARS else text
    prompt = f"""Review this source evaluation for potential upgrade.

TEXT EXCERPT:
{truncated}

HEURISTIC RESULT: {heuristic_permission}
CHECKS SUMMARY: {core_checks_summary}

The heuristics rated this as "Context-only". Review if it could be upgraded to:
- "B: Usable with safeguards" (has attribution, some verifiable claims)
- Keep as "C: Context-only" (opinion, analysis without strong evidence)

Respond ONLY with JSON:
{{"permission": "B_SAFEGUARDS|C_CONTEXT", "reason": "brief explanation"}}"""

    result = llm_review(client, prompt, model)
    if result and "permission" in result:
        return result["permission"], result.get("reason", "LLM assessment")
    return None


# -----------------------------------------------------------------------------
# Fetching
# -----------------------------------------------------------------------------
def cache_paths(cache_dir: str, url: str) -> Tuple[str, str]:
    h = sha256_hex(url)
    return os.path.join(cache_dir, f"{h}.json"), os.path.join(cache_dir, f"{h}.txt")


def read_cache(cache_dir: str, url: str, max_age_s: int) -> Optional[FetchedDoc]:
    meta_path, text_path = cache_paths(cache_dir, url)
    if not os.path.exists(meta_path):
        return None
    try:
        age = time.time() - os.stat(meta_path).st_mtime
        if max_age_s >= 0 and age > max_age_s:
            return None
        meta = json.loads(open(meta_path, "r", encoding="utf-8").read())
        text = ""
        if os.path.exists(text_path):
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
        meta["html"] = ""
        meta["text"] = ""
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(doc.text or "")
    except Exception:
        pass


def extract_from_html(html: str, url: str) -> Tuple[Dict[str, str], str, str]:
    """Extract metadata and text from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {}

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
    meta["author"] = get_meta("author") or get_prop("article:author") or ""

    # Remove noise
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    for tagname in ["nav", "footer", "header", "aside", "form", "button"]:
        for t in soup.find_all(tagname):
            t.decompose()

    # Kill common noise patterns
    for t in soup.find_all(True):
        try:
            if not hasattr(t, 'get') or not hasattr(t, 'decompose'):
                continue
            tag_id = t.get("id") or ""
            tag_class = t.get("class") or []
            if isinstance(tag_class, str):
                tag_class = [tag_class]
            ident = " ".join([str(tag_id), " ".join(str(c) for c in tag_class)]).lower()
            noise_kw = ["nav", "menu", "subscribe", "cookie", "newsletter", "advert", "banner", "modal", "social", "comment"]
            if any(k in ident for k in noise_kw):
                t.decompose()
        except (AttributeError, TypeError):
            continue

    # Try JSON-LD
    main_text = ""
    try:
        for sc in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
            raw = (sc.string or sc.get_text() or "").strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    body = data.get("articleBody") or data.get("text") or ""
                    if isinstance(body, str) and len(body) > len(main_text):
                        main_text = body
            except Exception:
                pass
    except Exception:
        pass

    # Try <article>
    if not main_text or len(main_text) < 500:
        art = soup.find("article")
        if art:
            art_text = art.get_text(separator="\n")
            if len(art_text) > len(main_text):
                main_text = art_text

    # Try readability
    if HAS_READABILITY and (not main_text or len(main_text) < 500):
        try:
            doc = Document(html)
            summary = doc.summary(html_partial=True)
            s2 = BeautifulSoup(summary, "html.parser")
            readable = s2.get_text(separator="\n")
            if len(readable) > len(main_text):
                main_text = readable
        except Exception:
            pass

    # Fallback to full body
    if not main_text or len(main_text) < 200:
        main_text = soup.get_text(separator="\n")

    return meta, title, clean_text(main_text)


def fetch_doc(
    session: requests.Session,
    url: str,
    cache_dir: str,
    sleep_s: float,
    timeout_s: int,
    cache_max_age_s: int,
    no_cache: bool,
) -> FetchedDoc:
    """Fetch a document."""
    if not no_cache:
        cached = read_cache(cache_dir, url, cache_max_age_s)
        if cached:
            return cached

    doc = FetchedDoc(url=url, fetched_at=utc_now_iso())
    doc.domain = registrable_domain(url)

    try:
        resp = session.get(url, headers=HEADERS, timeout=timeout_s, allow_redirects=True)
        doc.status_code = resp.status_code
        doc.final_url = str(resp.url)
        doc.content_type = resp.headers.get("content-type", "")
        doc.bytes_downloaded = len(resp.content or b"")

        if resp.status_code >= 400:
            doc.fetch_status = "http_error"
            doc.warnings.append(f"HTTP {resp.status_code}")
        elif "pdf" in doc.content_type.lower() or doc.final_url.lower().endswith(".pdf"):
            doc.fetch_status = "pdf"
            if HAS_PDFMINER:
                try:
                    ensure_dir(cache_dir)
                    pdf_path = os.path.join(cache_dir, f"{sha256_hex(url)}.pdf")
                    with open(pdf_path, "wb") as f:
                        f.write(resp.content)
                    doc.text = clean_text(pdf_extract_text(pdf_path) or "")
                except Exception as e:
                    doc.warnings.append(f"PDF extraction failed: {e}")
            else:
                doc.warnings.append("PDF extraction unavailable (pdfminer not installed)")
        else:
            resp.encoding = resp.encoding or "utf-8"
            html = resp.text or ""
            doc.html = html
            meta, title, text = extract_from_html(html, doc.final_url or url)
            doc.meta = meta
            doc.title = title
            doc.author = meta.get("author", "")
            doc.published = meta.get("published_time", "")
            doc.text = text
            doc.fetch_status = "ok"

            # Check for paywall/botblock
            combined = normalize(html + " " + text)
            if any(normalize(h) in combined for h in BOTBLOCK_HINTS):
                doc.warnings.append("Bot-block/anti-automation detected")
            if any(normalize(h) in combined for h in PAYWALL_HINTS):
                doc.warnings.append("Paywall/login wall detected")

    except requests.Timeout:
        doc.fetch_status = "timeout"
        doc.warnings.append("Request timed out")
    except Exception as e:
        doc.fetch_status = "error"
        doc.warnings.append(f"Fetch error: {e}")

    if not no_cache:
        write_cache(cache_dir, url, doc)
    time.sleep(sleep_s)
    return doc


def crawl_publisher_pages(
    session: requests.Session,
    main: FetchedDoc,
    cache_dir: str,
    sleep_s: float,
    timeout_s: int,
    cache_max_age_s: int,
    no_cache: bool,
    max_pages: int = 3,
) -> List[FetchedDoc]:
    """Crawl publisher's about/policy pages."""
    if not main.final_url:
        return []

    parsed = urlparse(main.final_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    pages = []
    for path in CRAWL_PATHS:
        if len(pages) >= max_pages:
            break
        doc = fetch_doc(session, urljoin(root, path), cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache)
        if doc.fetch_status in ("ok", "pdf") and len(doc.text or "") > 150:
            pages.append(doc)

    return pages


# -----------------------------------------------------------------------------
# Core Checks (Part 1)
# -----------------------------------------------------------------------------
def assess_relationship(url: str, domain: str, text: str) -> Tuple[RelationshipType, bool, str]:
    """
    Check 2: Relationship / self-interest restriction.
    Returns (relationship_type, a_only_restriction, reason)

    Key principle: Self-interest is about whether the source is making claims
    ABOUT ITSELF, not about whether it's an organization. A human rights org
    reporting on a government is third-party, not self-interest.
    """
    url_low = url.lower()
    domain_low = domain.lower()

    # Self-interest: ONLY about/self-description pages
    # These are pages where an org describes itself, its mission, its accomplishments
    if "/about" in url_low or "who-we-are" in url_low or "/about-us" in url_low:
        return RelationshipType.SELF_INTEREST, True, "Source is the organization's own about/self-description page"

    # Official/state sources - government domains
    if ".gov" in domain_low or ".mil" in domain_low or ".gouv" in domain_low:
        return RelationshipType.OFFICIAL_STATE, True, "Official government/state source - treat claims as narrative unless corroborated"

    # State media - known state-controlled outlets
    state_media = ["xinhua", "globaltimes", "rt.com", "sputnik", "presstv", "cgtn", "chinadaily"]
    if any(sm in domain_low for sm in state_media):
        return RelationshipType.OFFICIAL_STATE, True, "State-affiliated media - treat claims as narrative"

    # Default: third-party (let LLM review if needed for edge cases)
    return RelationshipType.THIRD_PARTY, False, "Third-party source - potentially eligible for B use"


def assess_completeness(doc: FetchedDoc) -> Tuple[Completeness, str]:
    """
    Check 3: Access & completeness.

    Key principle: Access failure != credibility failure.
    If we got substantial content, it's complete even if there were
    bot-block hints somewhere in the page (they might be in boilerplate
    we cleaned out).
    """
    if doc.fetch_status in ("http_error", "timeout", "error"):
        return Completeness.FAILED, f"Fetch failed: {doc.fetch_status}"

    text_len = len(doc.text or "")

    if text_len < 100:
        return Completeness.FAILED, f"Insufficient text retrieved ({text_len} chars)"

    # Check for partial access indicators
    has_paywall = any("paywall" in w.lower() or "login" in w.lower() for w in doc.warnings)
    has_botblock = any("bot" in w.lower() for w in doc.warnings)

    # If we have substantial content (>2000 chars), consider it complete
    # even if there were bot-block hints - we clearly got through
    if text_len >= 2000:
        if has_botblock or has_paywall:
            return Completeness.COMPLETE, f"Full content retrieved ({text_len} chars) despite access warnings"
        return Completeness.COMPLETE, f"Full content retrieved ({text_len} chars)"

    # For shorter content, access warnings matter more
    if has_botblock:
        return Completeness.PARTIAL, f"Bot-block detected with limited content ({text_len} chars)"

    if has_paywall and text_len < 800:
        return Completeness.PARTIAL, "Paywall detected with limited content retrieved"

    if text_len < 300:
        return Completeness.PARTIAL, f"Limited text retrieved ({text_len} chars)"

    return Completeness.COMPLETE, f"Full content retrieved ({text_len} chars)"


def assess_evidence_strength(doc: FetchedDoc) -> Tuple[EvidenceStrength, str, List[str]]:
    """
    Check 4: Evidence strength (anchor type).
    """
    if not doc.text or len(doc.text) < 100:
        return EvidenceStrength.NOT_ASSESSED, "Insufficient text to assess", []

    text = doc.text
    text_low = text.lower()

    # Check for primary anchors
    primary_found = []
    for kw in PRIMARY_ANCHOR_KEYWORDS:
        if kw in text_low:
            primary_found.append(kw)

    if doc.fetch_status == "pdf":
        quotes = find_quotes(text, primary_found[:3] if primary_found else ["document"], max_quotes=2)
        return EvidenceStrength.STRONG, "Primary document (PDF) with direct evidence", quotes

    if len(primary_found) >= 2:
        quotes = find_quotes(text, primary_found[:3], max_quotes=2)
        return EvidenceStrength.STRONG, f"Contains primary anchors: {', '.join(primary_found[:3])}", quotes

    # Check for secondary reporting with attribution
    attribution_patterns = [
        r"according to [A-Z]",
        r"[A-Z][a-z]+ (said|told|reported|stated)",
        r"sources? (said|told|confirmed)",
        r"citing [a-z]",
        r"documents? (show|reveal|indicate)",
    ]

    has_attribution = any(re.search(pat, text) for pat in attribution_patterns)

    if has_attribution:
        quotes = find_quotes(text, ["according to", "said", "told", "reported", "citing"], max_quotes=2)
        return EvidenceStrength.MEDIUM, "Secondary reporting with attribution", quotes

    return EvidenceStrength.WEAK, "Assertions without clear evidence trail", []


def assess_specificity(doc: FetchedDoc) -> Tuple[bool, str, List[str]]:
    """
    Check 5: Specificity & auditability.
    Look for: who/what/when/where/how much

    A source is specific and auditable if it provides concrete anchors
    that allow independent verification.
    """
    if not doc.text or len(doc.text) < 100:
        return False, "Insufficient text to assess", []

    text = doc.text
    anchors = []

    # WHEN: dates (years, full dates, relative time references)
    years = re.findall(r"\b(19\d\d|20\d\d)\b", text)
    full_dates = re.findall(
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4}\b",
        text, re.I
    )
    date_patterns = re.findall(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", text)
    if years or full_dates or date_patterns:
        total_dates = len(set(years)) + len(full_dates) + len(date_patterns)
        if total_dates >= 2:
            anchors.append(f"dates/times ({total_dates} found)")

    # WHERE: locations (countries, cities, regions, specific places)
    # Look for capitalized place names followed by location context
    location_context = re.findall(
        r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s*(?:Province|City|District|Region|County|State|Ministry|Bureau|Court|Parliament|Congress|Hall)\b",
        text
    )
    # Also catch standalone well-known location patterns
    countries_cities = re.findall(
        r"\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b",
        text
    )
    if location_context or len(countries_cities) >= 3:
        anchors.append(f"locations ({len(location_context) + min(len(countries_cities), 5)} found)")

    # HOW MUCH: numbers/quantities (votes, percentages, money, counts)
    # Look for numbers with context
    quantities_people = re.findall(
        r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:people|persons|individuals|members|delegates|votes?|cases|incidents|deaths|victims|detainees|prisoners|percent|%)\b",
        text, re.I
    )
    quantities_money = re.findall(
        r"\b(?:\$|€|£|¥)?\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:billion|million|trillion|yuan|dollars?|euros?|pounds?)\b",
        text, re.I
    )
    quantities_ratio = re.findall(r"\b\d+\s*(?:to|vs\.?|against)\s*\d+\b", text, re.I)
    total_quantities = len(quantities_people) + len(quantities_money) + len(quantities_ratio)
    if total_quantities >= 2:
        anchors.append(f"quantities ({total_quantities} found)")

    # WHO: named individuals
    # Pattern 1: "Name Name said/told/stated" or "Name Name,"
    named_speakers = re.findall(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\s*(?:,|said|told|stated|added|noted|warned|announced)", text)
    # Pattern 2: Quoted speech followed by attribution
    quote_attributions = re.findall(r'"\s*(?:said|told|according to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', text)
    # Pattern 3: "according to Name" or "Name, the/a title"
    titled_names = re.findall(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+),?\s+(?:the\s+)?(?:president|minister|director|chief|head|leader|spokesman|spokesperson|secretary|chairman|official)", text, re.I)
    unique_names = set(named_speakers + quote_attributions + titled_names)
    if len(unique_names) >= 2:
        anchors.append(f"named actors ({len(unique_names)} found)")

    # Require at least 2 different types of anchors for specificity
    has_specificity = len(anchors) >= 2

    if has_specificity:
        return True, f"Contains traceable anchors: {'; '.join(anchors)}", anchors
    elif anchors:
        return False, f"Limited anchors: {'; '.join(anchors)}", anchors
    else:
        return False, "No clear who/what/when/where/how much anchors found", []


def assess_severity_support(doc: FetchedDoc) -> Tuple[bool, SeveritySupport, str, List[str]]:
    """
    Check 7: Severity support gate.
    Only applies when claim is "systematic/widespread/state policy".
    """
    if not doc.text:
        return False, SeveritySupport.NOT_ASSESSED, "No text to assess", []

    text_low = doc.text.lower()

    # First check if this is even a systematic claim
    has_systematic_claim = any(kw in text_low for kw in SYSTEMATIC_CLAIM_KEYWORDS)

    if not has_systematic_claim:
        return False, SeveritySupport.NOT_APPLICABLE, "No systematic/widespread claims detected", []

    # Check the three requirements
    missing = []

    has_extent = any(h in text_low for h in EXTENT_HINTS)
    has_systematicity = any(h in text_low for h in SYSTEMATICITY_HINTS)
    has_institutionalization = any(h in text_low for h in INSTITUTIONALIZATION_HINTS)

    if not has_extent:
        missing.append("extent (severity of harm)")
    if not has_systematicity:
        missing.append("systematicity (pattern/frequency)")
    if not has_institutionalization:
        missing.append("institutionalization (state apparatus)")

    if not missing:
        return True, SeveritySupport.SUPPORTED, "Supports systematic claim: extent + systematicity + institutionalization present", []
    elif len(missing) <= 1:
        return True, SeveritySupport.PARTIAL, f"Partial severity support. Missing: {', '.join(missing)}", missing
    else:
        return True, SeveritySupport.PARTIAL, f"Claim appears systematic but evidence incomplete. Missing: {', '.join(missing)}", missing


def assess_publisher_signals(aux_pages: List[FetchedDoc]) -> PublisherSignals:
    """
    Part 2: Optional publisher signals.
    Only assess if evidence is actually found. Otherwise "Not assessed".
    """
    signals = PublisherSignals()

    if not aux_pages:
        return signals

    combined_text = "\n\n".join([p.text for p in aux_pages if p.text])
    combined_low = combined_text.lower()

    # 8) Ownership/transparency
    ownership_keywords = ["owned by", "ownership", "board of directors", "governance", "nonprofit", "non-profit", "funded by", "funding"]
    if any(kw in combined_low for kw in ownership_keywords):
        quotes = find_quotes(combined_text, ownership_keywords, max_quotes=2)
        signals.ownership_transparency = Check(
            status="found",
            reason="Ownership/governance information found on publisher pages",
            evidence_quotes=quotes,
            assessed=True
        )

    # 9) Corrections behavior
    corrections_keywords = ["correction", "retraction", "we correct", "clarification", "erratum", "updated"]
    if any(kw in combined_low for kw in corrections_keywords):
        quotes = find_quotes(combined_text, corrections_keywords, max_quotes=2)
        signals.corrections_behavior = Check(
            status="found",
            reason="Corrections/accountability policy or practice found",
            evidence_quotes=quotes,
            assessed=True
        )

    # 10) Standards/method
    standards_keywords = ["editorial standards", "editorial policy", "code of ethics", "methodology", "fact-check", "verification"]
    if any(kw in combined_low for kw in standards_keywords):
        quotes = find_quotes(combined_text, standards_keywords, max_quotes=2)
        signals.standards_transparency = Check(
            status="found",
            reason="Editorial standards or methodology information found",
            evidence_quotes=quotes,
            assessed=True
        )

    return signals


# -----------------------------------------------------------------------------
# Use Permission Determination
# -----------------------------------------------------------------------------
def determine_use_permission(
    intended_use: IntendedUse,
    core: CoreChecks,
    publisher: PublisherSignals,
    is_single_source: bool,
    domain: str = "",
) -> Tuple[UsePermission, str]:
    """
    Determine final use permission based on all checks.
    """
    reasons = []

    # Rule: Wikipedia is a tertiary source - not suitable for B use
    if "wikipedia.org" in domain.lower():
        if intended_use == IntendedUse.B:
            return UsePermission.C_CONTEXT, "Wikipedia is a tertiary source - use for context only, cite primary sources for factual claims"
        return UsePermission.C_CONTEXT, "Wikipedia - tertiary source for context/background"

    # Rule: Access failure caps B use
    if core.completeness == Completeness.FAILED:
        return UsePermission.MANUAL_RETRIEVAL, "Fetch failed - manual retrieval needed before assessment"

    if core.completeness == Completeness.PARTIAL:
        if intended_use == IntendedUse.B:
            return UsePermission.MANUAL_RETRIEVAL, "Partial content retrieved - manual retrieval needed for B use"

    # Rule: Self-interest/official sources default to A-only
    if core.a_only_restriction:
        if intended_use == IntendedUse.B:
            return UsePermission.A_NARRATIVE, f"Self-interest/official source: {core.relationship_reason}"
        elif intended_use == IntendedUse.A:
            return UsePermission.A_NARRATIVE, "Valid for narrative use (what the source claims)"

    # For B (factual) use
    if intended_use == IntendedUse.B:
        # Check evidence strength
        if core.evidence_strength == EvidenceStrength.WEAK:
            return UsePermission.C_CONTEXT, "Evidence too weak for factual support - use as context only"

        # Check specificity
        if not core.has_specificity:
            return UsePermission.C_CONTEXT, "Lacks traceable anchors (who/what/when/where) - use as context only"

        # Check severity gate if applicable
        if core.severity_claim_detected and core.severity_support == SeveritySupport.PARTIAL:
            if core.severity_missing:
                reasons.append(f"Severity claim detected but missing: {', '.join(core.severity_missing)}")

        # Determine B level
        if core.evidence_strength == EvidenceStrength.STRONG:
            if core.completeness == Completeness.COMPLETE and core.has_specificity:
                # Check corroboration for high-impact claims
                if is_single_source:
                    return UsePermission.B_SAFEGUARDS, "Strong evidence but single-source run - corroboration not assessed"
                return UsePermission.B_PREFERRED, "Strong primary anchors, complete access, traceable"

        # Medium evidence
        if core.evidence_strength == EvidenceStrength.MEDIUM:
            return UsePermission.B_SAFEGUARDS, "Secondary reporting - must corroborate for key claims"

        return UsePermission.C_CONTEXT, "Insufficient evidence strength for factual support"

    # For C (analysis/context) use
    if intended_use == IntendedUse.C:
        if core.completeness in (Completeness.COMPLETE, Completeness.PARTIAL):
            return UsePermission.C_CONTEXT, "Valid for analysis/background context"
        return UsePermission.MANUAL_RETRIEVAL, "Insufficient content for context use"

    # For A (narrative) use
    if intended_use == IntendedUse.A:
        if core.completeness in (Completeness.COMPLETE, Completeness.PARTIAL):
            return UsePermission.A_NARRATIVE, "Valid for narrative use (cite as 'X said...')"
        return UsePermission.MANUAL_RETRIEVAL, "Insufficient content to extract narrative"

    return UsePermission.DO_NOT_USE, "Could not determine appropriate use"


def check_auto_reject(doc: FetchedDoc) -> Tuple[bool, str]:
    """Check if source should be auto-rejected (satire, spam, etc.)."""
    domain = doc.domain.lower()

    # Known satire
    if domain in KNOWN_SATIRE_DOMAINS:
        return True, f"Known satire site: {domain}"

    # Satire signals in page
    if doc.text or doc.title:
        combined = normalize((doc.title or "") + " " + (doc.meta.get("description", "") or ""))
        if any(normalize(kw) in combined for kw in SATIRE_KEYWORDS):
            return True, "Satire/parody signals detected in page metadata"

    return False, ""


# -----------------------------------------------------------------------------
# Main Evaluation
# -----------------------------------------------------------------------------
def evaluate_source(
    session: requests.Session,
    url: str,
    intended_use: IntendedUse,
    cache_dir: str,
    sleep_s: float,
    timeout_s: int,
    cache_max_age_s: int,
    no_cache: bool,
    max_aux_pages: int,
    is_single_source: bool,
    llm_client: Optional[Any] = None,
    llm_model: str = DEFAULT_LLM_MODEL,
) -> EvalResult:
    """Evaluate a single source."""

    # Fetch main document
    main = fetch_doc(session, url, cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache)

    result = EvalResult(
        url=url,
        final_url=main.final_url or url,
        domain=main.domain,
        fetch_status=main.fetch_status,
        content_type=main.content_type,
        text_length=len(main.text or ""),
        warnings=list(main.warnings),
    )

    # Check auto-reject first
    should_reject, reject_reason = check_auto_reject(main)

    # LLM augmentation: verify satire detection on borderline cases
    if not should_reject and llm_client and main.text:
        # Check if content might be satirical even without keyword matches
        llm_satire = llm_assess_satire(llm_client, main.title or "", main.text, main.domain, llm_model)
        if llm_satire and llm_satire[0]:
            should_reject = True
            reject_reason = f"LLM detected satire: {llm_satire[1]}"
            result.llm_used = True
            result.llm_decisions.append("satire_detection")

    if should_reject:
        result.use_permission = UsePermission.DO_NOT_USE
        result.permission_reason = reject_reason
        return result

    # Fetch auxiliary publisher pages
    aux_pages = []
    if main.fetch_status in ("ok", "pdf") and max_aux_pages > 0:
        aux_pages = crawl_publisher_pages(
            session, main, cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache, max_aux_pages
        )

    result.evidence_pages = [main.final_url or url] + [p.final_url or p.url for p in aux_pages]

    # Run core checks
    core = CoreChecks()
    core.intended_use = intended_use

    # Check 2: Relationship
    rel, a_only, rel_reason = assess_relationship(main.final_url or url, main.domain, main.text or "")
    core.relationship = rel
    core.a_only_restriction = a_only
    core.relationship_reason = rel_reason

    # Check 3: Completeness
    comp, comp_reason = assess_completeness(main)
    core.completeness = comp
    core.completeness_reason = comp_reason

    # Check 4: Evidence strength
    ev_strength, ev_reason, ev_quotes = assess_evidence_strength(main)
    core.evidence_strength = ev_strength
    core.evidence_reason = ev_reason
    core.evidence_quotes = ev_quotes

    # Check 5: Specificity
    has_spec, spec_reason, spec_anchors = assess_specificity(main)
    core.has_specificity = has_spec
    core.specificity_reason = spec_reason
    core.specificity_anchors = spec_anchors

    # Check 6: Corroboration
    if is_single_source:
        core.corroboration = CorroborationStatus.NOT_ASSESSED
        core.corroboration_reason = "Single-source run - corroboration not assessed"
    else:
        # In multi-source runs, this would be populated by cross-checking
        core.corroboration = CorroborationStatus.NOT_ASSESSED
        core.corroboration_reason = "Cross-source corroboration check not implemented in this run"

    # Check 7: Severity support
    sev_detected, sev_support, sev_reason, sev_missing = assess_severity_support(main)
    core.severity_claim_detected = sev_detected
    core.severity_support = sev_support
    core.severity_reason = sev_reason
    core.severity_missing = sev_missing

    result.core = core

    # Part 2: Publisher signals
    result.publisher = assess_publisher_signals(aux_pages)

    # LLM Augmentation: review borderline cases
    if llm_client and main.text:
        # Review evidence strength if weak or medium
        if core.evidence_strength in (EvidenceStrength.WEAK, EvidenceStrength.MEDIUM):
            llm_ev = llm_assess_evidence_strength(
                llm_client, main.text, core.evidence_strength.value, llm_model
            )
            if llm_ev:
                new_strength, new_reason = llm_ev
                if new_strength != core.evidence_strength.value:
                    core.evidence_strength = EvidenceStrength(new_strength)
                    core.evidence_reason = f"{core.evidence_reason} [LLM: {new_reason}]"
                    result.llm_used = True
                    result.llm_decisions.append("evidence_strength")

        # Review self-interest if third-party (might have missed it)
        if core.relationship == RelationshipType.THIRD_PARTY:
            llm_self = llm_assess_self_interest(
                llm_client, main.final_url or url, main.text, llm_model
            )
            if llm_self and llm_self[0]:
                core.relationship = RelationshipType.SELF_INTEREST
                core.a_only_restriction = True
                core.relationship_reason = f"LLM detected self-interest: {llm_self[1]}"
                result.llm_used = True
                result.llm_decisions.append("self_interest")

        # Review severity support if partial
        if core.severity_claim_detected and core.severity_support == SeveritySupport.PARTIAL:
            llm_sev = llm_assess_severity_support(
                llm_client, main.text, core.severity_missing, llm_model
            )
            if llm_sev:
                status, reason, still_missing = llm_sev
                if status == "supported":
                    core.severity_support = SeveritySupport.SUPPORTED
                    core.severity_reason = f"{core.severity_reason} [LLM: {reason}]"
                    core.severity_missing = []
                elif status == "partial":
                    core.severity_reason = f"{core.severity_reason} [LLM: {reason}]"
                    core.severity_missing = still_missing
                result.llm_used = True
                result.llm_decisions.append("severity_support")

    # Determine final use permission
    use_perm, perm_reason = determine_use_permission(intended_use, core, result.publisher, is_single_source, main.domain)

    # LLM final review: check if C: Context-only could be upgraded
    # Skip upgrade review for Wikipedia (tertiary source - always context-only)
    if llm_client and main.text and use_perm == UsePermission.C_CONTEXT and "wikipedia.org" not in main.domain.lower():
        checks_summary = f"evidence={core.evidence_strength.value}, specificity={core.has_specificity}, relationship={core.relationship.value}"
        llm_final = llm_final_review(
            llm_client, main.text, use_perm.value, checks_summary, llm_model
        )
        if llm_final:
            new_perm, new_reason = llm_final
            if new_perm == "B_SAFEGUARDS":
                use_perm = UsePermission.B_SAFEGUARDS
                perm_reason = f"{perm_reason} [LLM upgraded: {new_reason}]"
                result.llm_used = True
                result.llm_decisions.append("final_review_upgrade")

    result.use_permission = use_perm
    result.permission_reason = perm_reason

    return result


def evaluate_sources(
    urls: List[str],
    intended_use: str,
    cache_dir: str,
    cache_max_age_s: int,
    no_cache: bool,
    sleep_s: float,
    timeout_s: int,
    max_aux_pages: int,
    use_llm: bool = True,
    llm_model: str = DEFAULT_LLM_MODEL,
) -> List[EvalResult]:
    """Evaluate multiple sources."""
    session = requests.Session()

    # Initialize LLM client if enabled
    llm_client = None
    if use_llm:
        llm_client = get_anthropic_client()
        if llm_client:
            print(f"LLM augmentation enabled (model: {llm_model})")
        else:
            print("LLM augmentation requested but ANTHROPIC_API_KEY not set or anthropic not installed")

    # Dedupe URLs
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    is_single_source = len(unique_urls) == 1
    use = IntendedUse(intended_use)

    results = []
    for i, url in enumerate(unique_urls, 1):
        print(f"[{i}/{len(unique_urls)}] Evaluating: {url}")
        result = evaluate_source(
            session=session,
            url=url,
            intended_use=use,
            cache_dir=cache_dir,
            sleep_s=sleep_s,
            timeout_s=timeout_s,
            cache_max_age_s=cache_max_age_s,
            no_cache=no_cache,
            max_aux_pages=max_aux_pages,
            is_single_source=is_single_source,
            llm_client=llm_client,
            llm_model=llm_model,
        )
        print(f"    -> {result.use_permission.value}")
        results.append(result)

    return results


# -----------------------------------------------------------------------------
# Report Generation
# -----------------------------------------------------------------------------
def render_report_md(results: List[EvalResult]) -> str:
    """Generate markdown report."""
    lines = []
    lines.append("# HRF Source Evaluation Report")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
    lines.append(f"_Evaluator: v6 (HRF Practical v1)_\n")

    for r in results:
        lines.append(f"## {r.final_url or r.url}\n")
        lines.append(f"**Domain:** {r.domain}")
        lines.append(f"**Use Permission:** {r.use_permission.value}")
        lines.append(f"**Reason:** {r.permission_reason}\n")

        lines.append("### Core Checks\n")

        lines.append(f"1. **Intended Use:** {r.core.intended_use.value}")

        lines.append(f"2. **Relationship:** {r.core.relationship.value}")
        lines.append(f"   - A-only restriction: {r.core.a_only_restriction}")
        lines.append(f"   - Reason: {r.core.relationship_reason}")

        lines.append(f"3. **Completeness:** {r.core.completeness.value}")
        lines.append(f"   - {r.core.completeness_reason}")

        lines.append(f"4. **Evidence Strength:** {r.core.evidence_strength.value}")
        lines.append(f"   - {r.core.evidence_reason}")
        if r.core.evidence_quotes:
            for q in r.core.evidence_quotes:
                lines.append(f"   - Evidence: \"{q}\"")

        lines.append(f"5. **Specificity:** {'Yes' if r.core.has_specificity else 'No'}")
        lines.append(f"   - {r.core.specificity_reason}")

        lines.append(f"6. **Corroboration:** {r.core.corroboration.value}")
        lines.append(f"   - {r.core.corroboration_reason}")

        lines.append(f"7. **Severity Support:** {r.core.severity_support.value}")
        if r.core.severity_claim_detected:
            lines.append(f"   - {r.core.severity_reason}")
            if r.core.severity_missing:
                lines.append(f"   - Missing: {', '.join(r.core.severity_missing)}")

        lines.append("\n### Publisher Signals (Optional)\n")
        lines.append(f"8. **Ownership Transparency:** {r.publisher.ownership_transparency.status}")
        if r.publisher.ownership_transparency.assessed:
            lines.append(f"   - {r.publisher.ownership_transparency.reason}")

        lines.append(f"9. **Corrections Behavior:** {r.publisher.corrections_behavior.status}")
        if r.publisher.corrections_behavior.assessed:
            lines.append(f"   - {r.publisher.corrections_behavior.reason}")

        lines.append(f"10. **Standards Transparency:** {r.publisher.standards_transparency.status}")
        if r.publisher.standards_transparency.assessed:
            lines.append(f"   - {r.publisher.standards_transparency.reason}")

        if r.warnings:
            lines.append("\n### Warnings")
            for w in r.warnings:
                lines.append(f"- {w}")

        if r.llm_used:
            lines.append("\n### LLM Augmentation")
            lines.append(f"- LLM used: Yes")
            lines.append(f"- Decisions augmented: {', '.join(r.llm_decisions)}")

        lines.append("\n### Evidence Pages Fetched")
        for ep in r.evidence_pages:
            lines.append(f"- {ep}")

        lines.append("\n---\n")

    return "\n".join(lines)


def result_to_dict(r: EvalResult) -> Dict[str, Any]:
    """Convert result to JSON-serializable dict."""
    return {
        "url": r.url,
        "final_url": r.final_url,
        "domain": r.domain,
        "use_permission": r.use_permission.value,
        "permission_reason": r.permission_reason,
        "core_checks": {
            "intended_use": r.core.intended_use.value,
            "relationship": {
                "type": r.core.relationship.value,
                "a_only_restriction": r.core.a_only_restriction,
                "reason": r.core.relationship_reason,
            },
            "completeness": {
                "status": r.core.completeness.value,
                "reason": r.core.completeness_reason,
            },
            "evidence_strength": {
                "level": r.core.evidence_strength.value,
                "reason": r.core.evidence_reason,
                "quotes": r.core.evidence_quotes,
            },
            "specificity": {
                "has_anchors": r.core.has_specificity,
                "reason": r.core.specificity_reason,
                "anchors": r.core.specificity_anchors,
            },
            "corroboration": {
                "status": r.core.corroboration.value,
                "reason": r.core.corroboration_reason,
            },
            "severity_support": {
                "claim_detected": r.core.severity_claim_detected,
                "status": r.core.severity_support.value,
                "reason": r.core.severity_reason,
                "missing": r.core.severity_missing,
            },
        },
        "publisher_signals": {
            "ownership_transparency": {
                "status": r.publisher.ownership_transparency.status,
                "assessed": r.publisher.ownership_transparency.assessed,
                "reason": r.publisher.ownership_transparency.reason,
                "quotes": r.publisher.ownership_transparency.evidence_quotes,
            },
            "corrections_behavior": {
                "status": r.publisher.corrections_behavior.status,
                "assessed": r.publisher.corrections_behavior.assessed,
                "reason": r.publisher.corrections_behavior.reason,
                "quotes": r.publisher.corrections_behavior.evidence_quotes,
            },
            "standards_transparency": {
                "status": r.publisher.standards_transparency.status,
                "assessed": r.publisher.standards_transparency.assessed,
                "reason": r.publisher.standards_transparency.reason,
                "quotes": r.publisher.standards_transparency.evidence_quotes,
            },
        },
        "metadata": {
            "fetch_status": r.fetch_status,
            "content_type": r.content_type,
            "text_length": r.text_length,
            "evidence_pages": r.evidence_pages,
            "warnings": r.warnings,
            "llm_used": r.llm_used,
            "llm_error": r.llm_error,
            "llm_decisions": r.llm_decisions,
        },
    }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="HRF Source Evaluator v6 - Source Credibility Standard (Practical v1)"
    )
    p.add_argument("--works-cited", default="", help="Path to works cited file with URLs")
    p.add_argument("--urls", default="", help="Comma-separated URLs to evaluate")
    p.add_argument("--intended-use", required=True, choices=["A", "B", "C"],
                   help="A=narrative, B=factual support, C=analysis/context")
    p.add_argument("--cache-dir", default=".cache_hrf_eval")
    p.add_argument("--cache-max-age-s", type=int, default=7 * 24 * 3600)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--sleep-s", type=float, default=DEFAULT_SLEEP_S)
    p.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--max-aux-pages", type=int, default=3)
    p.add_argument("--out-md", default="hrf_report.md")
    p.add_argument("--out-json", default="hrf_report.json")
    p.add_argument("--no-llm", action="store_true", help="Disable LLM augmentation (heuristics only)")
    p.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                   help=f"Anthropic model for LLM review (default: {DEFAULT_LLM_MODEL})")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)

    urls = []
    if args.works_cited:
        text = open(args.works_cited, "r", encoding="utf-8", errors="ignore").read()
        urls.extend(extract_urls(text))
    if args.urls:
        urls.extend([u.strip() for u in args.urls.split(",") if u.strip().startswith("http")])

    if not urls:
        print("No URLs found. Provide --works-cited and/or --urls.")
        sys.exit(2)

    print(f"Evaluating {len(urls)} source(s) for intended use: {args.intended_use}\n")

    results = evaluate_sources(
        urls=urls,
        intended_use=args.intended_use,
        cache_dir=args.cache_dir,
        cache_max_age_s=args.cache_max_age_s,
        no_cache=args.no_cache,
        sleep_s=args.sleep_s,
        timeout_s=args.timeout_s,
        max_aux_pages=args.max_aux_pages,
        use_llm=not args.no_llm,
        llm_model=args.llm_model,
    )

    # Write outputs
    md = render_report_md(results)
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write(md)

    json_out = [result_to_dict(r) for r in results]
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2)

    print(f"\nWrote: {args.out_md}")
    print(f"Wrote: {args.out_json}")

    # Summary
    print("\n=== Summary ===")
    for r in results:
        print(f"  {r.domain}: {r.use_permission.value}")


if __name__ == "__main__":
    main()
