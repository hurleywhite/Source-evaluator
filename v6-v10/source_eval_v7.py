#!/usr/bin/env python3
"""
Source Evaluator v7 — Narrative Clustering Engine

Purpose: Replace binary source-level verdicts with narrative-driven research
aggregation. Organizes global reporting by topic and perspective, empowering
researchers to make their own analytical determinations.

Core shift:
  v6: Individual source → binary verdict (A/B/C/Manual/Do not use)
  v7: Batch sources → claim extraction → narrative clustering → source-tier tagging

Pipeline:
  1. Ingest: Batch URL fetch (reuses v6 fetch layer)
  2. Tag:   Source-tier classification (Trusted Intl / General News / State Media / Propaganda)
  3. Extract: LLM extracts atomic claims per article
  4. Cluster: LLM groups claims into narrative clusters per topic
  5. Render: Narrative cloud with source-tier breakdown per cluster

Design principle: The system does the months of gathering and organizing.
The researcher does the seconds of judgment.
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

import requests

# Reuse v6 fetch layer
from source_eval_v6 import (
    FetchedDoc,
    fetch_doc,
    sanitize_url,
    registrable_domain,
    extract_urls,
    clean_text,
    utc_now_iso,
    ensure_dir,
    HEADERS,
    USER_AGENT,
)

# Optional imports
try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    import tldextract
    HAS_TLDEXTRACT = True
except ImportError:
    HAS_TLDEXTRACT = False


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

DEFAULT_TIMEOUT_S = 25
DEFAULT_SLEEP_S = 0.5
DEFAULT_CACHE_DIR = ".cache_narrative"
DEFAULT_CACHE_MAX_AGE = 7 * 24 * 3600  # 7 days

# LLM models
CLAIM_EXTRACTION_MODEL = "claude-haiku-4-5-20251001"     # Fast, cheap — per-article extraction
NARRATIVE_CLUSTERING_MODEL = "claude-sonnet-4-20250514"  # Quality — cross-article clustering

# Max chars of article text to send to LLM for claim extraction
MAX_ARTICLE_CHARS = 8000


# =============================================================================
# Source Tier Classification
# =============================================================================

class SourceTier:
    TRUSTED_INTL = "trusted_international"
    GENERAL_NEWS = "general_news"
    STATE_MEDIA = "state_media"
    PROPAGANDA = "propaganda"
    UNCLASSIFIED = "unclassified"


TIER_LABELS = {
    SourceTier.TRUSTED_INTL: "Trusted International",
    SourceTier.GENERAL_NEWS: "General News",
    SourceTier.STATE_MEDIA: "State Media",
    SourceTier.PROPAGANDA: "Propaganda",
    SourceTier.UNCLASSIFIED: "Unclassified",
}

TIER_COLORS = {
    SourceTier.TRUSTED_INTL: "#1B7340",   # dark green
    SourceTier.GENERAL_NEWS: "#3B7CB8",   # blue
    SourceTier.STATE_MEDIA: "#C4880B",    # amber
    SourceTier.PROPAGANDA: "#B83B3B",     # red
    SourceTier.UNCLASSIFIED: "#6B7280",   # gray
}

# ── Built-in source tier registry ──
# This is a starter set. The full list will come from Alvaro + Kristen.
# Domains are matched against the registrable domain (no www prefix).

TRUSTED_INTERNATIONAL_DOMAINS = {
    # UN system
    "ohchr.org", "un.org", "unhcr.org", "unicef.org", "who.int",
    "ilo.org", "unesco.org", "undp.org",
    # Major HR organizations
    "amnesty.org", "hrw.org", "freedomhouse.org", "pen.org",
    "cpj.org", "rsf.org", "icrc.org", "icj-cij.org",
    "civicus.org", "article19.org", "fidh.org",
    # International courts & bodies
    "icc-cpi.int", "echr.coe.int",
    # Regional bodies
    "oas.org", "achpr.org", "coe.int",
    # Research / policy (nonpartisan)
    "cfr.org", "brookings.edu", "chathamhouse.org",
    "carnegieendowment.org", "crisisgroup.org",
    "transparency.org", "globalwitness.org",
    # US government human rights reporting
    "state.gov", "cecc.gov", "uscirf.gov",
    "congress.gov", "justice.gov",
    # Other government HR bodies
    "dfat.gov.au",
    # Specialized monitors
    "monitor.civicus.org", "business-humanrights.org",
    "icnl.org", "lawfaremedia.org",
    # Academic / legal
    "scholarship.law.upenn.edu", "harvardlawreview.org",
    "journals.sagepub.com", "papers.ssrn.com",
    "bfi.uchicago.edu",
    # Specific to HRF work
    "hrf.org", "safeguarddefenders.com",
    "duihua.org", "duihuahrjournal.org",
}

GENERAL_NEWS_DOMAINS = {
    # Wire services
    "apnews.com", "reuters.com",
    # Major intl outlets
    "bbc.com", "bbc.co.uk", "theguardian.com", "nytimes.com",
    "washingtonpost.com", "economist.com", "ft.com",
    "wsj.com", "bloomberg.com", "cnn.com",
    "aljazeera.com",  # Generally trusted except on Qatar topics
    # Regional quality outlets
    "scmp.com", "latimes.com", "npr.org",
    "wired.com", "forbes.com", "businessinsider.com",
    "cbc.ca", "abc.net.au",
    # Specialist / independent
    "thediplomat.com", "rfa.org", "rferl.org",
    "voanews.com",  # US-funded but factual reporting
    "chinafile.com", "chinamediaproject.org",
    "asiatimes.com", "qz.com",
    "motherjones.com", "commondreams.org",
    # Fact-checkers
    "mediabiasfactcheck.com", "snopes.com", "politifact.com",
    # Reference
    "wikipedia.org",
}

STATE_MEDIA_DOMAINS = {
    # China
    "chinadaily.com.cn": "China (state-owned)",
    "globaltimes.cn": "China (CCP-affiliated tabloid)",
    "xinhuanet.com": "China (state news agency)",
    "cgtn.com": "China (state broadcaster)",
    "news.cgtn.com": "China (state broadcaster)",
    "cctv.com": "China (state broadcaster)",
    "people.com.cn": "China (CCP organ)",
    "peopledaily.com.cn": "China (CCP organ)",
    "china.org.cn": "China (state portal)",
    "ecns.cn": "China (state news)",
    "bjreview.com.cn": "China (state magazine)",
    # Russia
    "tass.com": "Russia (state news agency)",
    "ria.ru": "Russia (state news agency)",
    # Iran
    "presstv.ir": "Iran (state broadcaster)",
    "irna.ir": "Iran (state news agency)",
    # Turkey
    "trtworld.com": "Turkey (state broadcaster)",
    "aa.com.tr": "Turkey (state news agency)",
    # Qatar
    # Note: Al Jazeera is in general_news but flagged for Qatar topics
    # Cuba
    "granma.cu": "Cuba (CCP organ)",
    "cubadebate.cu": "Cuba (state media)",
    # Venezuela
    "vtv.gob.ve": "Venezuela (state broadcaster)",
    # North Korea
    "kcna.kp": "North Korea (state news agency)",
    # Saudi Arabia
    "spa.gov.sa": "Saudi Arabia (state news agency)",
}

PROPAGANDA_DOMAINS = {
    # Russian propaganda
    "rt.com", "sputniknews.com", "sputnikglobe.com",
    # Venezuelan/Cuban propaganda
    "telesurtv.net", "prensa-latina.cu",
    # Chinese propaganda outlets (beyond state media)
    "en.people.cn",
    # Fringe / conspiracy
    "globalresearch.ca", "mintpressnews.com",
    # Socialist/far-left propaganda
    "wsws.org",
}

# Social media / user-generated — not classified, just flagged
SOCIAL_MEDIA_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "reddit.com", "tiktok.com", "substack.com",
    "medium.com",
}


def classify_source_tier(domain: str, url: str = "") -> Tuple[str, str]:
    """Classify a domain into source tiers.

    Returns (tier, note) where note provides context (e.g., regime type for
    state media).
    """
    domain_bare = domain.lower().removeprefix("www.")

    # Check propaganda first (overrides state media)
    if domain_bare in PROPAGANDA_DOMAINS:
        return SourceTier.PROPAGANDA, f"Known propaganda outlet: {domain_bare}"

    # Check state media (dict with regime notes)
    if domain_bare in STATE_MEDIA_DOMAINS:
        return SourceTier.STATE_MEDIA, STATE_MEDIA_DOMAINS[domain_bare]

    # Check trusted international
    if domain_bare in TRUSTED_INTERNATIONAL_DOMAINS:
        return SourceTier.TRUSTED_INTL, ""

    # Check general news
    if domain_bare in GENERAL_NEWS_DOMAINS:
        return SourceTier.GENERAL_NEWS, ""

    # Check social media — classify as general but flag
    if domain_bare in SOCIAL_MEDIA_DOMAINS:
        return SourceTier.GENERAL_NEWS, "Social media / user-generated content"

    # Government domains default to trusted international
    if any(domain_bare.endswith(g) for g in [".gov", ".mil", ".gouv.fr", ".gov.uk", ".gov.au"]):
        return SourceTier.TRUSTED_INTL, "Government domain"

    # Academic domains
    if any(domain_bare.endswith(g) for g in [".edu", ".ac.uk", ".edu.au", ".ac.jp"]):
        return SourceTier.TRUSTED_INTL, "Academic institution"

    return SourceTier.UNCLASSIFIED, ""


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class SourceArticle:
    """A fetched and classified source article."""
    url: str
    domain: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    text: str = ""
    text_length: int = 0
    tier: str = SourceTier.UNCLASSIFIED
    tier_note: str = ""
    fetch_status: str = ""
    fetch_warnings: List[str] = field(default_factory=list)


@dataclass
class Claim:
    """An atomic factual claim extracted from a source."""
    text: str                    # The claim itself
    source_url: str              # Which article it came from
    source_domain: str = ""
    source_tier: str = ""
    source_title: str = ""
    date_reference: str = ""     # When the claimed event occurred
    actors: List[str] = field(default_factory=list)  # Who is involved
    claim_type: str = ""         # "event", "policy", "statistic", "allegation", etc.


@dataclass
class NarrativeCluster:
    """A group of related claims forming a narrative."""
    id: str                       # Unique cluster ID
    topic: str                    # High-level topic (e.g., "Press Freedom")
    narrative: str                # Short narrative description
    description: str              # Research-ready factual description
    claims: List[Claim] = field(default_factory=list)
    source_urls: List[str] = field(default_factory=list)
    source_count: int = 0
    tier_breakdown: Dict[str, int] = field(default_factory=dict)
    convergence_signal: str = ""  # "strong" if trusted + state agree
    coverage_flag: str = ""       # "low_independent" if dominated by state/propaganda


@dataclass
class NarrativeMap:
    """The complete output: all narrative clusters organized by topic."""
    country: str = ""
    total_sources: int = 0
    sources_fetched: int = 0
    sources_failed: int = 0
    topics: Dict[str, List[NarrativeCluster]] = field(default_factory=dict)
    source_articles: List[SourceArticle] = field(default_factory=list)
    generated_at: str = ""


# =============================================================================
# LLM Integration
# =============================================================================

def get_anthropic_client() -> Optional[Anthropic]:
    if not HAS_ANTHROPIC:
        return None
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return Anthropic(api_key=key)


def llm_extract_claims(
    client: Anthropic,
    article: SourceArticle,
) -> List[Dict[str, Any]]:
    """Extract atomic claims from a single article using LLM.

    Returns a list of claim dicts with keys:
      claim, date_reference, actors, claim_type
    """
    text_snippet = article.text[:MAX_ARTICLE_CHARS]
    if not text_snippet or len(text_snippet) < 100:
        return []

    prompt = f"""You are a research assistant extracting factual claims from news articles about human rights, governance, and civil liberties.

ARTICLE:
Title: {article.title or '(no title)'}
Source: {article.domain}
Published: {article.published or 'unknown'}
URL: {article.url}

TEXT:
{text_snippet}

INSTRUCTIONS:
Extract discrete, atomic factual claims from this article. Each claim should be:
- A single factual assertion (not a summary or opinion)
- Specific enough to be verified or contested
- Written in neutral, research-ready language

For each claim, provide:
- "claim": The factual assertion in one sentence
- "date_reference": When this event/fact occurred (e.g., "March 2023", "2020", or "ongoing")
- "actors": Key actors involved (people, organizations, governments) as a list
- "claim_type": One of: "event", "policy", "legal_action", "statistic", "allegation", "statement", "institutional"

Return ONLY a JSON array. Extract 3-8 claims (focus on the most significant, verifiable assertions).
If the article is an about page or organizational description, extract claims about the organization's role and activities.
Do NOT editorialize or add analytical conclusions.

JSON array:"""

    try:
        resp = client.messages.create(
            model=CLAIM_EXTRACTION_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Extract JSON array from response
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            claims = json.loads(match.group())
            return claims if isinstance(claims, list) else []
        return []
    except Exception as e:
        log.warning(f"  Claim extraction failed for {article.domain}: {e}")
        return []


def llm_cluster_narratives(
    client: Anthropic,
    all_claims: List[Claim],
    country: str = "",
) -> List[Dict[str, Any]]:
    """Group all extracted claims into narrative clusters by topic.

    Returns a list of cluster dicts.
    """
    if not all_claims:
        return []

    # Build claims summary for the LLM
    claims_text = ""
    for i, c in enumerate(all_claims):
        claims_text += f"[{i}] ({c.source_domain}, {c.source_tier}) {c.text}\n"
        if c.date_reference:
            claims_text += f"    Date: {c.date_reference}\n"
        if c.actors:
            claims_text += f"    Actors: {', '.join(c.actors)}\n"

    # Truncate if too long (Sonnet can handle 200K but be reasonable)
    if len(claims_text) > 50000:
        claims_text = claims_text[:50000] + "\n... (truncated)"

    prompt = f"""You are a research analyst organizing factual claims into narrative clusters for human rights researchers.

CONTEXT: These claims were extracted from {len(set(c.source_url for c in all_claims))} sources about {country or 'a country/region'}.

CLAIMS:
{claims_text}

INSTRUCTIONS:
Group these claims into narrative clusters organized by TOPIC, then by PERSPECTIVE within each topic.

For each cluster, provide:
- "topic": High-level topic (e.g., "Press Freedom", "Judicial Independence", "Transnational Repression", "NGO Restrictions", "Political Prisoners", "Internet Censorship", "Constitutional Framework")
- "narrative": A short label for this specific narrative thread (e.g., "Systematic jailing of journalists", "Government claims of judicial reform")
- "description": A 2-3 sentence factual description in research-ready language. Use the format: "It was reported that [event] occurred in the context of [broader situation]." NO analytical conclusions.
- "claim_indices": List of claim indices [0, 3, 7, ...] that belong to this cluster
- "coverage_note": If this narrative is supported mostly by state/propaganda sources with no independent corroboration, note "low_independent_coverage". If trusted and state sources agree, note "cross_tier_convergence". Otherwise leave empty.

Guidelines:
- Create 4-12 clusters (avoid too many small ones or too few large ones)
- Each claim should appear in exactly one cluster
- Group by the STORY being told, not by source
- If claims conflict (e.g., "elections were free" vs "opposition was barred"), they should be in SEPARATE clusters under the SAME topic
- Do NOT make analytical determinations — just organize

Return ONLY a JSON array of cluster objects.

JSON array:"""

    try:
        resp = client.messages.create(
            model=NARRATIVE_CLUSTERING_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            clusters = json.loads(match.group())
            return clusters if isinstance(clusters, list) else []
        return []
    except Exception as e:
        log.error(f"  Narrative clustering failed: {e}")
        return []


# =============================================================================
# Pipeline
# =============================================================================

def fetch_all_articles(
    urls: List[str],
    cache_dir: str = DEFAULT_CACHE_DIR,
    sleep_s: float = DEFAULT_SLEEP_S,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    cache_max_age_s: int = DEFAULT_CACHE_MAX_AGE,
    no_cache: bool = False,
) -> List[SourceArticle]:
    """Step 1: Fetch all URLs and classify their source tiers."""

    session = requests.Session()
    session.headers.update(HEADERS)
    articles = []

    for i, url in enumerate(urls):
        url = sanitize_url(url)
        log.info(f"[{i+1}/{len(urls)}] Fetching: {url[:80]}...")

        doc = fetch_doc(session, url, cache_dir, sleep_s, timeout_s, cache_max_age_s, no_cache)
        domain = doc.domain or registrable_domain(url)

        # Classify source tier
        tier, tier_note = classify_source_tier(domain, url)

        article = SourceArticle(
            url=url,
            domain=domain,
            title=doc.title or "",
            author=doc.author or "",
            published=doc.published or "",
            text=doc.text or "",
            text_length=len(doc.text or ""),
            tier=tier,
            tier_note=tier_note,
            fetch_status=doc.fetch_status or "unknown",
            fetch_warnings=doc.warnings,
        )
        articles.append(article)

        status = "OK" if doc.fetch_status == "ok" else doc.fetch_status
        log.info(f"    -> {status} | {TIER_LABELS.get(tier, tier)} | {len(doc.text or '')} chars")

    return articles


def extract_all_claims(
    client: Anthropic,
    articles: List[SourceArticle],
) -> List[Claim]:
    """Step 3: Extract atomic claims from all fetched articles."""
    all_claims = []

    fetchable = [a for a in articles if a.text and len(a.text) >= 100]
    log.info(f"\nExtracting claims from {len(fetchable)} articles...")

    for i, article in enumerate(fetchable):
        log.info(f"  [{i+1}/{len(fetchable)}] {article.domain}: {article.title[:60]}...")

        raw_claims = llm_extract_claims(client, article)

        for rc in raw_claims:
            if not isinstance(rc, dict) or "claim" not in rc:
                continue
            claim = Claim(
                text=rc["claim"],
                source_url=article.url,
                source_domain=article.domain,
                source_tier=article.tier,
                source_title=article.title,
                date_reference=rc.get("date_reference", ""),
                actors=rc.get("actors", []) if isinstance(rc.get("actors"), list) else [],
                claim_type=rc.get("claim_type", ""),
            )
            all_claims.append(claim)

        log.info(f"    -> {len(raw_claims)} claims extracted")

    log.info(f"\nTotal claims extracted: {len(all_claims)}")
    return all_claims


def cluster_claims(
    client: Anthropic,
    claims: List[Claim],
    articles: List[SourceArticle],
    country: str = "",
) -> NarrativeMap:
    """Step 4-5: Cluster claims into narrative groups and build the map."""

    log.info(f"\nClustering {len(claims)} claims into narrative groups...")

    raw_clusters = llm_cluster_narratives(client, claims, country)

    narrative_map = NarrativeMap(
        country=country,
        total_sources=len(articles),
        sources_fetched=len([a for a in articles if a.fetch_status == "ok"]),
        sources_failed=len([a for a in articles if a.fetch_status != "ok"]),
        generated_at=utc_now_iso(),
        source_articles=articles,
    )

    for rc in raw_clusters:
        if not isinstance(rc, dict):
            continue

        topic = rc.get("topic", "Uncategorized")
        claim_indices = rc.get("claim_indices", [])

        # Gather claims for this cluster
        cluster_claims = []
        cluster_sources = set()
        tier_counts = {}

        for idx in claim_indices:
            if 0 <= idx < len(claims):
                c = claims[idx]
                cluster_claims.append(c)
                cluster_sources.add(c.source_url)
                tier_counts[c.source_tier] = tier_counts.get(c.source_tier, 0) + 1

        # Determine convergence signal
        has_trusted = tier_counts.get(SourceTier.TRUSTED_INTL, 0) > 0
        has_general = tier_counts.get(SourceTier.GENERAL_NEWS, 0) > 0
        has_state = tier_counts.get(SourceTier.STATE_MEDIA, 0) > 0
        has_propaganda = tier_counts.get(SourceTier.PROPAGANDA, 0) > 0

        independent_count = tier_counts.get(SourceTier.TRUSTED_INTL, 0) + tier_counts.get(SourceTier.GENERAL_NEWS, 0)
        state_count = tier_counts.get(SourceTier.STATE_MEDIA, 0) + tier_counts.get(SourceTier.PROPAGANDA, 0)

        convergence = ""
        coverage_flag = rc.get("coverage_note", "")

        if (has_trusted or has_general) and has_state:
            convergence = "cross_tier_convergence"
        if state_count > 0 and independent_count == 0:
            coverage_flag = "low_independent_coverage"

        cluster = NarrativeCluster(
            id=f"{topic.lower().replace(' ', '_')}_{len(narrative_map.topics.get(topic, []))+1}",
            topic=topic,
            narrative=rc.get("narrative", ""),
            description=rc.get("description", ""),
            claims=cluster_claims,
            source_urls=list(cluster_sources),
            source_count=len(cluster_sources),
            tier_breakdown=tier_counts,
            convergence_signal=convergence,
            coverage_flag=coverage_flag,
        )

        if topic not in narrative_map.topics:
            narrative_map.topics[topic] = []
        narrative_map.topics[topic].append(cluster)

    log.info(f"Created {sum(len(v) for v in narrative_map.topics.values())} clusters across {len(narrative_map.topics)} topics")
    return narrative_map


# =============================================================================
# Output Serialization
# =============================================================================

def narrative_map_to_dict(nm: NarrativeMap) -> Dict[str, Any]:
    """Convert NarrativeMap to JSON-serializable dict."""
    topics_out = {}
    for topic, clusters in nm.topics.items():
        topics_out[topic] = []
        for c in clusters:
            topics_out[topic].append({
                "id": c.id,
                "topic": c.topic,
                "narrative": c.narrative,
                "description": c.description,
                "claims": [
                    {
                        "text": cl.text,
                        "source_url": cl.source_url,
                        "source_domain": cl.source_domain,
                        "source_tier": cl.source_tier,
                        "source_title": cl.source_title,
                        "date_reference": cl.date_reference,
                        "actors": cl.actors,
                        "claim_type": cl.claim_type,
                    }
                    for cl in c.claims
                ],
                "source_urls": c.source_urls,
                "source_count": c.source_count,
                "tier_breakdown": {
                    TIER_LABELS.get(k, k): v
                    for k, v in c.tier_breakdown.items()
                },
                "convergence_signal": c.convergence_signal,
                "coverage_flag": c.coverage_flag,
            })

    sources_out = []
    for a in nm.source_articles:
        sources_out.append({
            "url": a.url,
            "domain": a.domain,
            "title": a.title,
            "author": a.author,
            "published": a.published,
            "text_length": a.text_length,
            "tier": a.tier,
            "tier_label": TIER_LABELS.get(a.tier, a.tier),
            "tier_note": a.tier_note,
            "fetch_status": a.fetch_status,
            "fetch_warnings": a.fetch_warnings,
        })

    return {
        "country": nm.country,
        "total_sources": nm.total_sources,
        "sources_fetched": nm.sources_fetched,
        "sources_failed": nm.sources_failed,
        "topic_count": len(nm.topics),
        "cluster_count": sum(len(v) for v in nm.topics.values()),
        "generated_at": nm.generated_at,
        "topics": topics_out,
        "sources": sources_out,
    }


def render_narrative_md(nm: NarrativeMap) -> str:
    """Render narrative map as markdown report."""
    lines = []
    lines.append(f"# Narrative Map: {nm.country or 'Research Sources'}")
    lines.append(f"\n*Generated: {nm.generated_at}*")
    lines.append(f"\n**Sources:** {nm.total_sources} total | {nm.sources_fetched} fetched | {nm.sources_failed} failed")
    lines.append(f"**Topics:** {len(nm.topics)} | **Clusters:** {sum(len(v) for v in nm.topics.values())}")

    # Source tier summary
    tier_counts = {}
    for a in nm.source_articles:
        tier_counts[a.tier] = tier_counts.get(a.tier, 0) + 1
    lines.append("\n## Source Tier Breakdown")
    for tier, count in sorted(tier_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- **{TIER_LABELS.get(tier, tier)}**: {count}")

    # Topics and clusters
    for topic, clusters in nm.topics.items():
        lines.append(f"\n## {topic}")
        lines.append(f"*{len(clusters)} narrative thread(s)*\n")

        for cluster in clusters:
            # Flag indicators
            flags = []
            if cluster.convergence_signal == "cross_tier_convergence":
                flags.append("[CONVERGENCE]")
            if cluster.coverage_flag == "low_independent_coverage":
                flags.append("[LOW INDEPENDENT COVERAGE]")
            flag_str = " ".join(flags)

            lines.append(f"### {cluster.narrative} {flag_str}")
            lines.append(f"\n{cluster.description}\n")

            # Source breakdown
            tier_str = " | ".join(
                f"{TIER_LABELS.get(k, k)}: {v}"
                for k, v in cluster.tier_breakdown.items()
            )
            lines.append(f"**Sources ({cluster.source_count}):** {tier_str}")

            # Claims
            lines.append("\n**Key claims:**")
            for claim in cluster.claims:
                date_str = f" ({claim.date_reference})" if claim.date_reference else ""
                lines.append(f"- {claim.text}{date_str} — *{claim.source_domain}*")

            lines.append("")

    return "\n".join(lines)


# =============================================================================
# Main Pipeline Entry Point
# =============================================================================

def run_narrative_pipeline(
    urls: List[str],
    country: str = "",
    cache_dir: str = DEFAULT_CACHE_DIR,
    sleep_s: float = DEFAULT_SLEEP_S,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    no_cache: bool = False,
    out_json: str = "",
    out_md: str = "",
) -> NarrativeMap:
    """Run the full narrative clustering pipeline."""

    # Step 1: Fetch all articles
    log.info(f"Fetching {len(urls)} source(s)...\n")
    articles = fetch_all_articles(
        urls, cache_dir, sleep_s, timeout_s,
        DEFAULT_CACHE_MAX_AGE, no_cache,
    )

    fetched = [a for a in articles if a.text and len(a.text) >= 100]
    failed = [a for a in articles if not a.text or len(a.text) < 100]
    log.info(f"\nFetch complete: {len(fetched)} succeeded, {len(failed)} failed")

    if failed:
        for a in failed:
            log.info(f"  FAILED: {a.url} ({a.fetch_status})")

    # Step 2: Get LLM client
    client = get_anthropic_client()
    if not client:
        log.error("ANTHROPIC_API_KEY not set. Cannot extract claims or cluster narratives.")
        # Return map with just source data (no claims/clusters)
        nm = NarrativeMap(
            country=country,
            total_sources=len(articles),
            sources_fetched=len(fetched),
            sources_failed=len(failed),
            generated_at=utc_now_iso(),
            source_articles=articles,
        )
        _write_outputs(nm, out_json, out_md)
        return nm

    # Step 3: Extract claims
    all_claims = extract_all_claims(client, articles)

    # Step 4-5: Cluster and build narrative map
    nm = cluster_claims(client, all_claims, articles, country)

    # Write outputs
    _write_outputs(nm, out_json, out_md)

    return nm


def _write_outputs(nm: NarrativeMap, out_json: str, out_md: str):
    """Write JSON and MD output files."""
    if out_json:
        ensure_dir(os.path.dirname(out_json) or ".")
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(narrative_map_to_dict(nm), f, ensure_ascii=False, indent=2)
        log.info(f"\nWrote: {out_json}")

    md_path = out_md or "narrative_report.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_narrative_md(nm))
    log.info(f"Wrote: {md_path}")


# =============================================================================
# CLI
# =============================================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Source Evaluator v7 — Narrative Clustering")
    p.add_argument("--works-cited", required=True, help="Path to file with URLs (one per line or works-cited format)")
    p.add_argument("--country", default="", help="Country/region name for context")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    p.add_argument("--out-json", default="", help="Output JSON path")
    p.add_argument("--out-md", default="", help="Output Markdown path")
    p.add_argument("--sleep-s", type=float, default=DEFAULT_SLEEP_S)
    p.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--no-cache", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # Read URLs
    with open(args.works_cited, "r", encoding="utf-8") as f:
        raw = f.read()
    urls = extract_urls(raw)
    if not urls:
        log.error("No URLs found in input file.")
        sys.exit(1)

    log.info(f"Source Evaluator v7 — Narrative Clustering")
    log.info(f"Found {len(urls)} URL(s) in {args.works_cited}")
    if args.country:
        log.info(f"Country: {args.country}")
    log.info("")

    run_narrative_pipeline(
        urls=urls,
        country=args.country,
        cache_dir=args.cache_dir,
        sleep_s=args.sleep_s,
        timeout_s=args.timeout_s,
        no_cache=args.no_cache,
        out_json=args.out_json,
        out_md=args.out_md,
    )


if __name__ == "__main__":
    main()
