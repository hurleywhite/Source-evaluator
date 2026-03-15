# Source Evaluator: Validation Protocol

## Purpose

This document establishes procedures for tracking the accuracy of the Source Evaluator and ensuring its classifications remain reliable over time.

---

## Initial Calibration

### Step 1: Create Ground Truth Sample

**Sample Size:** 50-100 sources representing all permission categories

**Selection Criteria:**
| Category | Target Count | Selection Method |
|----------|--------------|------------------|
| B: Preferred evidence | 15-20 | Known reliable sources (NGOs, major news) |
| B: Usable with safeguards | 10-15 | Secondary reporting with attribution |
| C: Context-only | 10-15 | Wikipedia, opinion pieces, analysis |
| A: Narrative-only | 10-15 | State media, advocacy orgs, government sites |
| Do not use | 5-10 | Satire, forums, known unreliable |

**Include "Sentinel" Sources:**
These are sources with unambiguous correct answers:
- `theonion.com` → Must be "Do not use"
- `hrw.org/news/...` → Must be "B: Preferred"
- `chinadaily.com.cn` → Must be "A: Narrative-only"
- `en.wikipedia.org/...` → Must be "C: Context-only"
- `reddit.com/...` → Must be "Do not use"

### Step 2: Human Expert Classification

**Process:**
1. Two or more independent reviewers classify each source
2. Reviewers do NOT see tool output beforehand
3. Each reviewer assigns one of six use-permissions
4. Disagreements resolved by third reviewer or discussion

**Classification Guidelines for Reviewers:**
- Consider: Is this source making claims about itself?
- Consider: Is this state-controlled media?
- Consider: Does this have primary documents or just assertions?
- Consider: Are claims specific and verifiable?

### Step 3: Calculate Inter-Rater Reliability

**Cohen's Kappa Calculation:**
```
κ = (Po - Pe) / (1 - Pe)

Where:
Po = observed agreement (% of sources where reviewers agree)
Pe = expected agreement by chance
```

**Interpretation:**
| Kappa | Interpretation |
|-------|----------------|
| < 0.20 | Poor |
| 0.21-0.40 | Fair |
| 0.41-0.60 | Moderate |
| 0.61-0.80 | Substantial |
| 0.81-1.00 | Almost perfect |

**Target:** κ ≥ 0.60 (substantial agreement)

---

## Tool vs. Human Comparison

### Accuracy Metrics

| Metric | Formula | Target |
|--------|---------|--------|
| **Overall Agreement** | (Tool matches consensus) / Total | ≥ 80% |
| **False Positive Rate** | Tool says "Preferred" but humans say worse / All "Preferred" | ≤ 10% |
| **False Negative Rate** | Tool says "Narrative" but humans say "Preferred" / All "Narrative" | ≤ 10% |
| **Over-Caution Rate** | "Manual retrieval" but accessible / All "Manual retrieval" | ≤ 30% |

### Category-Specific Accuracy

Track precision and recall for each category:

**Precision:** Of sources the tool labeled X, what % are correctly X?
```
Precision = True Positives / (True Positives + False Positives)
```

**Recall:** Of sources that should be X, what % did the tool label X?
```
Recall = True Positives / (True Positives + False Negatives)
```

**Target by Category:**

| Category | Target Precision | Target Recall |
|----------|-----------------|---------------|
| B: Preferred evidence | ≥ 85% | ≥ 80% |
| A: Narrative-only | ≥ 90% | ≥ 85% |
| Do not use | ≥ 95% | ≥ 90% |
| C: Context-only | ≥ 75% | ≥ 70% |

---

## Ongoing Monitoring

### Monthly Spot-Check Protocol

**Sample:** 20 randomly selected sources from recent evaluations

**Process:**
1. Export random sample from recent runs
2. Single reviewer classifies each source independently
3. Compare to tool output
4. Document disagreements with rationale

**Documentation Template:**

| Source | Tool Result | Human Result | Match? | Notes |
|--------|-------------|--------------|--------|-------|
| [URL] | B: Preferred | B: Preferred | ✓ | |
| [URL] | A: Narrative | B: Preferred | ✗ | Tool missed third-party reporting |

### Sentinel Source Validation

**Per-Run Check:** Include 5 sentinel sources in every evaluation run

| Sentinel | Expected Result | If Wrong |
|----------|-----------------|----------|
| theonion.com article | Do not use | Critical failure - investigate |
| freedomhouse.org report | B: Preferred | Review self-interest logic |
| globaltimes.cn article | A: Narrative-only | Review state media detection |
| wikipedia.org article | C: Context-only | Review tertiary source logic |
| reddit.com thread | Do not use | Review unreliable source logic |

### User Feedback Loop

**Track Researcher Overrides:**

When a researcher disagrees with tool output:
1. Document the source URL
2. Record tool recommendation
3. Record researcher's decision
4. Record rationale for override

**Override Tracking Template:**

| Date | Source | Tool Said | Researcher Chose | Rationale |
|------|--------|-----------|------------------|-----------|
| | | | | |

**Monthly Review:**
- Calculate override rate: Overrides / Total sources evaluated
- Target: ≤ 15% override rate
- If higher: Investigate patterns, adjust tool logic

---

## LLM Drift Detection

### Quarterly Recalibration

**Process:**
1. Re-run the original 100-source calibration set
2. Compare results to original baseline
3. Flag any sources with changed classifications

**Drift Alert Thresholds:**

| Change | Action |
|--------|--------|
| 0-5 sources changed | Normal variance, document |
| 6-10 sources changed | Investigate, may need prompt adjustment |
| 11+ sources changed | Critical - halt use, full review |

### After LLM Model Updates

When Anthropic releases new Claude versions:
1. Run full calibration set before updating
2. Update to new model
3. Re-run calibration set
4. Compare results
5. Adjust prompts if needed to maintain accuracy

---

## Reporting

### Monthly Accuracy Report

```
SOURCE EVALUATOR ACCURACY REPORT
Period: [Month Year]

SPOT-CHECK RESULTS (n=20)
- Agreement rate: XX%
- Disagreements: X sources
  - [List disagreements with rationale]

OVERRIDE TRACKING
- Total sources evaluated: XXX
- Researcher overrides: XX (X%)
- Common override patterns:
  - [Pattern 1]
  - [Pattern 2]

SENTINEL VALIDATION
- All sentinels correct: Yes/No
- Failures: [List any]

RECOMMENDATIONS
- [Any suggested adjustments]
```

### Quarterly Calibration Report

```
QUARTERLY CALIBRATION REPORT
Period: [Q# Year]

GROUND TRUTH COMPARISON (n=100)
- Overall agreement: XX%
- By category:
  | Category | Precision | Recall |
  |----------|-----------|--------|
  | B: Preferred | XX% | XX% |
  | B: Usable | XX% | XX% |
  | C: Context | XX% | XX% |
  | A: Narrative | XX% | XX% |
  | Do not use | XX% | XX% |

LLM DRIFT CHECK
- Sources with changed classification: X
- Details: [List changes]

TREND ANALYSIS
- Accuracy trend: Improving / Stable / Declining
- Override rate trend: [%]

ACTION ITEMS
- [Required adjustments]
```

---

## Escalation Procedures

### When to Escalate

| Trigger | Action |
|---------|--------|
| Sentinel source fails | Immediate investigation, pause critical work |
| Agreement rate < 70% | Full calibration review |
| Override rate > 20% | Prompt/logic review |
| LLM drift > 10 sources | Model evaluation, possible rollback |

### Escalation Contacts

| Issue Type | Contact |
|------------|---------|
| Technical failures | [Technical lead] |
| Accuracy concerns | [Research lead] |
| Methodology questions | [Methodology advisor] |

---

*Last updated: [Date]*
