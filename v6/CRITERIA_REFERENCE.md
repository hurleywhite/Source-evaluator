# Source Evaluator v6 — Complete Criteria Reference

---

## Part 1: Core Checks (Required)

### 1. Intended Use
**What it is:** The purpose for which you're citing the source.

| Use | Purpose | Example |
|-----|---------|---------|
| **A** | Narrative — documenting what an actor *claims* | "The Chinese government stated..." |
| **B** | Factual support — proving what *actually happened* | "On July 9, 2015, authorities detained..." |
| **C** | Context — background, analysis, interpretation | "Scholars argue this reflects a broader pattern..." |

**Why it matters:** A government press release is valid for Use A (what they claim) but requires corroboration for Use B (proving facts).

---

### 2. Relationship / Self-Interest
**What it is:** The source's stake in the claim being made.

| Type | Description | Restriction |
|------|-------------|-------------|
| **Self-interest** | Source speaking about itself (about pages, org self-descriptions) | A-only |
| **Official/State** | Government sites, state media (Xinhua, CGTN, RT, Global Times) | A-only |
| **Third-party** | Independent source with no direct stake | Eligible for B |

**Why it matters:** An organization describing its own mission is inherently self-serving — cite as narrative, not fact.

---

### 3. Completeness (Access)
**What it is:** How much content we successfully retrieved.

| Status | Condition |
|--------|-----------|
| **Complete** | ≥2,000 characters retrieved, or 300–2,000 with no access warnings |
| **Partial** | Limited text with paywall/bot-block detected, or 100–300 characters |
| **Failed** | <100 characters, HTTP error, or timeout |

**Key principle:** Access failure ≠ credibility failure. A paywalled NYT article isn't unreliable — it just needs manual retrieval.

---

### 4. Evidence Strength
**What it is:** The type of evidence the source provides.

| Level | Indicators |
|-------|------------|
| **Strong** | Primary documents: laws, court rulings, indictments, official records, datasets, constitutions, verdicts |
| **Medium** | Secondary reporting with clear attribution: "according to [named source]," direct quotes, "citing [document]" |
| **Weak** | Assertions without evidence trail, opinion, analysis without sourcing |

**Primary anchor keywords detected:**

```
law, regulation, constitution, court, judgment, ruling, verdict, sentenced,
convicted, indictment, filing, official record, transcript, dataset, document,
decree, resolution, statute, ordinance
```

---

### 5. Specificity & Auditability
**What it is:** Whether the source provides concrete, verifiable details.

Requires **≥2 anchor types** from:

| Anchor | What We Look For |
|--------|------------------|
| **When** | Dates, years, time references ("January 15, 2024", "since 2017") |
| **Where** | Locations with context ("Xinjiang Province", "Beijing No. 2 Intermediate Court") |
| **How much** | Quantities ("2,952 to 0 vote", "over 300 lawyers", "1.8 million detained") |
| **Who** | Named individuals with roles ("Xi Jinping", "lawyer Wang Quanzhang", "Minister Chen") |

**Why it matters:** Specific claims can be independently verified. Vague claims cannot.

---

### 6. Corroboration
**What it is:** Whether key claims appear in multiple independent sources.

| Status | Meaning |
|--------|---------|
| **Corroborated** | Claim verified across sources (multi-source runs) |
| **Not corroborated** | Claim appears in only one source |
| **Not assessed** | Single-source run or check not implemented |

**Why it matters:** Single-source claims carry higher risk of error or manipulation.

---

### 7. Severity Support Gate
**What it is:** Additional evidence requirements for claims of systematic or widespread abuse.

**Triggered by keywords:**

```
systematic, widespread, state policy, government policy, nationwide, mass,
genocide, ethnic cleansing, crimes against humanity
```

**Three requirements for systematic claims:**

| Requirement | What It Means | Keywords Detected |
|-------------|---------------|-------------------|
| **Extent** | Scale/severity of harm | killed, died, detained, arrested, imprisoned, tortured, sentenced, disappeared, injured, displaced |
| **Systematicity** | Pattern over time, not isolated | systematic, widespread, routine, pattern, ongoing, since, over years, hundreds, thousands |
| **Institutionalization** | State apparatus involvement | law, regulation, policy, ministry, bureau, agency, court, security services, directive, campaign, official |

| Result | Condition |
|--------|-----------|
| **Supported** | All 3 present |
| **Partial** | 1–2 present |
| **Not applicable** | No systematic claim detected |

**Why it matters:** Claiming "genocide" requires more evidence than claiming a single arrest. The most serious allegations demand the strongest documentation.

---

## Part 2: Publisher Signals (Optional)

These are assessed only if we successfully crawl the publisher's auxiliary pages (about, editorial, corrections, etc.).

### 8. Ownership Transparency
**What it looks for:** Does the publisher disclose who owns/funds them?

```
owned by, ownership, board of directors, governance, nonprofit, funded by
```

**Why it matters:** Hidden ownership can indicate state control, conflicts of interest, or lack of accountability.

---

### 9. Corrections Behavior
**What it looks for:** Does the publisher acknowledge and fix errors?

```
correction, retraction, we correct, clarification, erratum, updated
```

**Why it matters:** Willingness to correct errors indicates commitment to accuracy over narrative.

---

### 10. Standards Transparency
**What it looks for:** Does the publisher disclose editorial standards or methodology?

```
editorial standards, editorial policy, code of ethics, methodology, fact-check, verification
```

**Why it matters:** Transparent methodology allows readers to evaluate how claims were verified.

---

## Final Use Permissions

Based on all checks, sources receive one of six labels:

| Permission | Meaning | Typical Triggers |
|------------|---------|------------------|
| **B: Preferred evidence** | Cite for factual claims | Strong evidence + complete + specific + third-party |
| **B: Usable with safeguards** | Cite with corroboration | Medium evidence, or single-source run |
| **C: Context-only** | Background/analysis only | Weak evidence, or lacks specificity |
| **A: Narrative-only** | Cite as "X claims..." | Self-interest or official source |
| **Manual retrieval needed** | Human must access | Fetch failed, paywall, bot-block |
| **Do not use** | Disqualified | Satire, parody, or critical failure |

---

## Auto-Reject Triggers

| Trigger | Action |
|---------|--------|
| Known satire domains (theonion.com, babylonbee.com, clickhole.com) | Do not use |
| Satire keywords in metadata ("satire", "parody", "humor") | Do not use |
| LLM detects satirical content | Do not use |

---

## LLM Augmentation (Optional)

When enabled, Claude reviews borderline cases:

| Check | When LLM Is Called |
|-------|-------------------|
| Evidence strength | Heuristics return weak or medium |
| Self-interest | Third-party detected but content may be self-serving |
| Satire | No keyword match but content seems suspicious |
| Severity support | Systematic claim with only partial support |
| Final review | Result is C: Context-only (could it be upgraded?) |

The LLM can upgrade or adjust classifications, but all decisions trace to evidence in the retrieved text.

---

## Decision Logic Summary

```
IF fetch failed:
    → Manual retrieval needed

IF satire detected:
    → Do not use

IF self-interest OR official/state source:
    → A: Narrative-only

IF intended use is B (factual support):
    IF evidence strength is weak:
        → C: Context-only
    IF lacks specificity (< 2 anchor types):
        → C: Context-only
    IF evidence is strong + complete + specific:
        → B: Preferred evidence (or B: Usable with safeguards if single-source)
    IF evidence is medium:
        → B: Usable with safeguards

IF intended use is C:
    → C: Context-only

IF intended use is A:
    → A: Narrative-only
```

---

*HRF Source Credibility Standard — Practical Implementation v1*
