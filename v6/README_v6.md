# HRF Source Evaluator v6

**HRF Source Credibility Standard (Practical v1)**

## Overview

This tool evaluates source credibility for Human Rights Foundation research using use-permission labels instead of numeric scores. It actually fetches and analyzes source content rather than relying on LLM knowledge.

## Use-Permission Labels

Instead of "credible/not credible", the tool outputs:

| Label | Meaning |
|-------|---------|
| **B: Preferred evidence** | Strong anchors + complete access; severity/corroboration gates satisfied |
| **B: Usable with safeguards** | Must corroborate; no overclaiming |
| **C: Context-only** | Analysis/background; not proof |
| **A: Narrative-only** | Self-interested/official claims; cite as "X said..." |
| **Manual retrieval needed** | Access/extraction incomplete; cap B use |
| **Do not use** | Satire/spam/irretrievable |

## Intended Uses

- **A** = Narrative (what an actor claims)
- **B** = Factual support (what can be verified happened)
- **C** = Analysis/context (interpretation)

A source can be valid for A or C even if not valid for B.

## Core Checks (Part 1) - Always Required

1. **Intended Use** - A/B/C classification with constraints
2. **Relationship / Self-Interest** - Self-interest → A-only unless corroborated
3. **Access & Completeness** - complete/partial/failed (access failure ≠ credibility failure)
4. **Evidence Strength** - strong (primary anchors) / medium (secondary) / weak (assertions)
5. **Specificity & Auditability** - who/what/when/where/how much
6. **Corroboration Status** - corroborated / not corroborated / not assessed (single-source)
7. **Severity Support Gate** - For systematic claims: extent + systematicity + institutionalization

## Publisher Signals (Part 2) - Optional

Only assessed if evidence is found; otherwise "Not assessed":

8. **Ownership Transparency**
9. **Corrections/Accountability Behavior**
10. **Standards/Method Transparency**

## Usage

```bash
# Evaluate sources from a works cited file
python source_eval_v6.py --works-cited works_cited.txt --intended-use B

# Evaluate specific URLs
python source_eval_v6.py --urls "https://example.com/article" --intended-use B

# Options
--intended-use A|B|C     # Required: what you're using the source for
--works-cited FILE       # Path to works cited file with URLs
--urls "URL1,URL2"       # Comma-separated URLs
--no-cache               # Skip cache, fetch fresh
--max-aux-pages N        # Max publisher pages to crawl (default: 3)
--out-md FILE            # Output markdown report (default: hrf_report.md)
--out-json FILE          # Output JSON report (default: hrf_report.json)
```

## Key Principles

1. **Evidence-driven** - Every decision points to retrieved evidence
2. **"Not assessed" is valid** - Honest about what can't be determined
3. **Access failure ≠ credibility failure** - Partial/failed → "manual retrieval needed"
4. **LLM enforces constraints** - Doesn't decide truth, produces audit trail
5. **Self-interest restriction** - Official/state sources default to A-only

## Example Output

```
=== Summary ===
  apnews.com: B: Preferred evidence
  theonion.com: Do not use
  tibet.net: A: Narrative-only
  east-turkistan.net: A: Narrative-only
  aljazeera.com: B: Preferred evidence
```

## Test Results (2026-01-28)

| Source | Permission | Reasoning |
|--------|------------|-----------|
| AP News (Xi Jinping article) | B: Preferred | Third-party, strong anchors (constitution, ruling), 34 named actors |
| The Onion | Do not use | Known satire site |
| PEN America (709 Crackdown) | B: Preferred | Strong anchors (law, regulation), severity gate passed |
| East Turkistan Gov in Exile | A: Narrative-only | Self-interest (about page), restricted to narrative |
| Al Jazeera (Sun Lijun) | B: Preferred | Strong anchors (law, court, ruling), editorial standards found |
| Ming Pao (COVID article) | C: Context-only | Redirected/paywalled, only 507 chars, weak evidence |
| Tibet.net | Manual retrieval | HTTP 403 (blocked), self-interest flagged |

## Dependencies

```
requests
beautifulsoup4
tldextract (optional, for better domain extraction)
readability-lxml (optional, for better text extraction)
pdfminer.six (optional, for PDF extraction)
```

## Files

- `source_eval_v6.py` - Main evaluator script
- `hrf_report.md` - Markdown report output
- `hrf_report.json` - JSON report output
- `outputs/` - Saved test outputs
