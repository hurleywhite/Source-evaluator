# Source Evaluator: Development Summary

## Purpose

A tool to systematically evaluate source credibility for human rights documentation, ensuring that evidentiary claims meet the rigorous standards required for legal and advocacy work.

---

## Evolution: v1 → v6

### v1–v2: Foundation
Established the core architecture: automated URL fetching, content extraction, and a 10-criterion scoring rubric (C1–C10) producing a 0–100 credibility score. Introduced the critical distinction between *fetchability* (can we access it?) and *credibility* (is the content reliable?). A paywalled source is not inherently unreliable—it simply requires manual retrieval.

### v3: Operational Hardening
Added resilience for real-world deployment: network timeouts, subprocess isolation for PDF extraction, checkpoint saves during long runs. The system could now process large citation lists without hanging on unresponsive servers or malformed documents.

### v4: Methodological Refinement
Introduced nuanced handling of incomplete evidence. Criteria that cannot be assessed from available text are excluded rather than penalized—preventing false condemnation of sources we simply couldn't fully retrieve. Strengthened the validation layer: any LLM-assisted scoring must cite evidence that actually exists in the fetched content.

### v5: Severity Claim Support
Added structured checks for systematic abuse claims—the highest-stakes assertions in human rights work. The system now verifies whether a source provides evidence of *extent* (scale of harm), *systematicity* (pattern over time), and *institutionalization* (state apparatus involvement). Claims of genocide or crimes against humanity require all three.

### v6: Decision-Ready Output
Replaced numeric scores with actionable use-permission labels aligned to research workflow:

| Label | Meaning |
|-------|---------|
| **B: Preferred evidence** | Strong primary anchors, complete access, traceable |
| **B: Usable with safeguards** | Secondary reporting—corroborate key claims |
| **C: Context-only** | Valid for background, not factual support |
| **A: Narrative-only** | Cite as "X claims..." (self-interest sources) |
| **Manual retrieval needed** | Fetch failed—human must access directly |
| **Do not use** | Satire, parody, or disqualifying issues |

Integrated Claude (Anthropic) for augmented review of borderline cases—while maintaining full heuristic fallback for offline or cost-sensitive runs.

---

## What Matters

**Defensibility.** Every determination traces to retrieved evidence—specific quotes, anchors, and structural signals. No black-box scoring.

**Proportionality.** The system distinguishes intended use. A government statement is valid for documenting official positions (Use A) but requires corroboration for factual claims (Use B).

**Severity-aware.** Systematic abuse claims trigger additional evidence requirements. The tool flags when documentation falls short of what such claims demand.

**Practical.** Outputs integrate directly into research workflow—spreadsheet exports, structured JSON, human-readable reports. Sources requiring manual attention are clearly identified.

---

## Current Capability

- Evaluates 100 sources in approximately 10 minutes (with LLM augmentation)
- Automatic detection of self-interest, state media, and satire
- Publisher signal extraction from about/editorial/corrections pages
- Full audit trail in JSON for every determination
- Works offline (heuristics-only mode) or with LLM enhancement

---

*Developed for the Human Rights Foundation's source credibility standards.*
