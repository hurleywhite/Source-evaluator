# HRF Source Credibility Standard (v6)

## Intended Use Categories

| Code | Use | Description |
|------|-----|-------------|
| **A** | Narrative | What an actor claims ("X said...") |
| **B** | Factual support | What can be verified happened |
| **C** | Analysis/context | Interpretation, background |

---

## Final Use Permission Labels

| Label | Meaning |
|-------|---------|
| **B: Preferred evidence** | Strong primary anchors, complete access, traceable |
| **B: Usable with safeguards** | Secondary reporting - must corroborate key claims |
| **C: Context-only** | Valid for analysis/background, not factual support |
| **A: Narrative-only** | Cite as "X said..." (self-interest/official sources) |
| **Manual retrieval needed** | Fetch failed - human must retrieve content |
| **Do not use** | Satire, parody, or auto-rejected |

---

## Core Checks (Part 1) — Always Required

### 1. Intended Use
Set by user via `--intended-use A|B|C`

### 2. Relationship / Self-Interest

| Type | A-Only Restriction | Trigger |
|------|-------------------|---------|
| `self_interest` | Yes | `/about` pages, org speaking about itself |
| `official_state` | Yes | `.gov`, `.mil`, ministry, bureau, state media (Xinhua, RT, CGTN, Global Times, Sputnik, PressTV) |
| `third_party` | No | Independent source — eligible for B use |

### 3. Completeness (Access)

| Status | Condition |
|--------|-----------|
| `complete` | ≥2000 chars retrieved (even with bot-block warnings) |
| `complete` | 300-2000 chars with no access warnings |
| `partial` | <2000 chars with bot-block detected |
| `partial` | <800 chars with paywall detected |
| `partial` | 100-300 chars |
| `failed` | <100 chars or HTTP error/timeout |

**Key principle:** Access failure ≠ credibility failure

### 4. Evidence Strength

| Level | Trigger |
|-------|---------|
| `strong` | PDF document, OR ≥2 primary anchor keywords found |
| `medium` | Attribution patterns detected ("according to X", "X said", "citing") |
| `weak` | Assertions without clear evidence trail |
| `not_assessed` | <100 chars text |

**Primary anchor keywords:**
```
law, regulation, constitution, court, judgment, ruling,
verdict, sentenced, convicted, indictment, filing,
official record, transcript, dataset, document, decree,
resolution, statute, ordinance
```

### 5. Specificity & Auditability

Requires **≥2 different anchor types** from:

| Anchor Type | Detection |
|-------------|-----------|
| **When** (dates/times) | Years (19xx/20xx), full dates, date patterns |
| **Where** (locations) | "Province", "City", "District", "Ministry", "Court", etc. |
| **How much** (quantities) | Numbers + people/votes/percent/dollars/deaths/etc. |
| **Who** (named actors) | "Name Name said/told", titled names, quote attributions |

### 6. Corroboration

| Status | Condition |
|--------|-----------|
| `not_assessed` | Single-source run (default) |
| `corroborated` | Cross-source check (not implemented in v6) |
| `not_corroborated` | Cross-source check failed |

### 7. Severity Support Gate

**Only applies when systematic/widespread claims detected.**

Keywords that trigger severity check:
```
systematic, widespread, state policy, government policy,
nationwide, mass, genocide, ethnic cleansing, crimes against humanity
```

**Three requirements for systematic claims:**

| Requirement | Keywords |
|-------------|----------|
| **Extent** (severity of harm) | killed, died, detained, arrested, imprisoned, tortured, sentenced, disappeared, injured, displaced |
| **Systematicity** (pattern) | systematic, widespread, routine, pattern, regular, ongoing, since, over years, hundreds, thousands |
| **Institutionalization** (state apparatus) | law, regulation, policy, ministry, bureau, agency, court, security services, state media, directive, campaign, official, government, party, state |

| Result | Condition |
|--------|-----------|
| `supported` | All 3 present |
| `partial` | 1-2 present |
| `not_applicable` | No systematic claim detected |

---

## Publisher Signals (Part 2) — Optional

Only assessed if auxiliary pages are crawled (about, editorial, ethics, etc.)

| Signal | Detection Keywords |
|--------|-------------------|
| **8. Ownership transparency** | owned by, ownership, board of directors, governance, nonprofit, funded by |
| **9. Corrections behavior** | correction, retraction, we correct, clarification, erratum, updated |
| **10. Standards transparency** | editorial standards, editorial policy, code of ethics, methodology, fact-check, verification |

---

## Decision Logic

```
IF fetch_failed:
    → Manual retrieval needed

IF satire_detected (theonion.com, babylonbee.com, clickhole.com, or satire keywords):
    → Do not use

IF self_interest OR official_state:
    IF intended_use == B:
        → A: Narrative-only
    ELSE:
        → A: Narrative-only

IF intended_use == B:
    IF evidence_strength == weak:
        → C: Context-only
    IF specificity == false:
        → C: Context-only
    IF evidence_strength == strong AND completeness == complete AND specificity == true:
        IF single_source_run:
            → B: Usable with safeguards
        ELSE:
            → B: Preferred evidence
    IF evidence_strength == medium:
        → B: Usable with safeguards
    ELSE:
        → C: Context-only

IF intended_use == C:
    → C: Context-only

IF intended_use == A:
    → A: Narrative-only
```

---

## Auto-Reject Domains

```
theonion.com
babylonbee.com
clickhole.com
```

**Satire keywords (in title/description):**
```
satire, parody, humor, humour, comedy site
```

---

## Crawled Publisher Pages

Up to 3 pages crawled from:
```
/about
/about-us
/contact
/editorial-policy
/editorial
/ethics
/code-of-ethics
/standards
/methodology
/methods
/corrections
/retractions
/terms
/privacy
/governance
/ownership
```

---

---

## LLM Augmentation (Anthropic Claude)

The evaluator can use Claude to improve accuracy on borderline cases.

### How It Works
1. **Heuristics run first** (fast, free)
2. **LLM reviews borderline cases** (~20-30% of sources)
3. **LLM can upgrade or adjust** classifications

### When LLM Is Called

| Check | Trigger |
|-------|---------|
| Evidence Strength | Heuristics return `weak` or `medium` |
| Self-Interest | Third-party source (might have missed self-interest) |
| Satire Detection | No keyword match (might be subtle satire) |
| Severity Support | Systematic claim with only `partial` support |
| Final Review | Result is `C: Context-only` (could it be upgraded?) |

### Enabling LLM

```bash
export ANTHROPIC_API_KEY="your-api-key"
python source_eval_v6.py --works-cited file.txt --intended-use B
```

### Disabling LLM

```bash
python source_eval_v6.py --works-cited file.txt --intended-use B --no-llm
```

### Choosing Model

```bash
# Use Haiku (faster, cheaper) - default
python source_eval_v6.py --works-cited file.txt --intended-use B --llm-model claude-3-haiku-20240307

# Use Sonnet (more capable)
python source_eval_v6.py --works-cited file.txt --intended-use B --llm-model claude-3-5-sonnet-20241022
```

### Cost Estimates
- **Haiku**: ~$0.001-0.005 per source
- **Sonnet**: ~$0.01-0.05 per source

---

## Usage

```bash
cd ~/Desktop/Source-evaluator/v6
source ../.venv/bin/activate
python source_eval_v6.py --works-cited <file> --intended-use <A|B|C> [options]
```

### Required Arguments
- `--works-cited` — Path to a text file containing URLs
- `--intended-use` — One of: `A`, `B`, or `C`

### Optional Arguments
| Flag | Default | Description |
|------|---------|-------------|
| `--urls` | | Comma-separated URLs (alternative to file) |
| `--out-md` | `hrf_report.md` | Output markdown file |
| `--out-json` | `hrf_report.json` | Output JSON file |
| `--cache-dir` | `.cache_hrf_eval` | Cache directory |
| `--no-cache` | false | Disable caching |
| `--sleep-s` | 0.8 | Delay between requests |
| `--timeout-s` | 25 | Request timeout |
| `--max-aux-pages` | 3 | Publisher pages to crawl |
| `--no-llm` | false | Disable LLM augmentation |
| `--llm-model` | `claude-3-haiku-20240307` | Anthropic model to use |
