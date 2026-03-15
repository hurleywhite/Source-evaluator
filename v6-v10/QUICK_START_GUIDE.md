# Source Evaluator: Quick Start Guide

## Prerequisites

- Python 3.9+
- Anthropic API key (for LLM augmentation)

## Setup

```bash
# Navigate to the Source-evaluator directory
cd /path/to/Source-evaluator

# Activate virtual environment
source .venv312/bin/activate

# Set API key
export ANTHROPIC_API_KEY="your-key-here"
```

---

## Basic Usage

### 1. Prepare Your Sources

Create a text file with URLs (one per line):

```
sources.txt
───────────
https://www.hrw.org/news/2024/01/15/example-article
https://www.bbc.com/news/world-12345678
https://freedomhouse.org/report/example
```

### 2. Run the Evaluator

```bash
cd v6

python source_eval_v6.py \
  --works-cited ../sources.txt \
  --intended-use B \
  --out-md report.md \
  --out-json report.json
```

### 3. Review Results

Open `report.md` for human-readable output, or `report.json` for full audit trail.

---

## Command Options

| Option | Description | Default |
|--------|-------------|---------|
| `--works-cited FILE` | Path to URL list | Required |
| `--intended-use A\|B\|C` | A=narrative, B=factual, C=context | Required |
| `--out-md FILE` | Markdown report output | `hrf_report.md` |
| `--out-json FILE` | JSON audit trail output | `hrf_report.json` |
| `--no-llm` | Disable LLM augmentation | LLM enabled |
| `--llm-model MODEL` | Anthropic model to use | `claude-3-haiku-20240307` |

---

## Interpreting Results

| Result | What It Means | What To Do |
|--------|---------------|------------|
| **B: Preferred evidence** | Strong, traceable source | Use for factual claims |
| **B: Usable with safeguards** | Good but needs backup | Find corroborating source |
| **C: Context-only** | Background material only | Don't use for factual claims |
| **A: Narrative-only** | Self-interest/state source | Cite as "X claims..." |
| **Manual retrieval needed** | Couldn't access content | Retrieve manually, re-evaluate |
| **Do not use** | Satire or unreliable | Exclude entirely |

---

## Quick Examples

**Evaluate for factual claims (Use B):**
```bash
python source_eval_v6.py --works-cited sources.txt --intended-use B
```

**Evaluate for narrative/attribution (Use A):**
```bash
python source_eval_v6.py --works-cited sources.txt --intended-use A
```

**Run without LLM (faster, less nuanced):**
```bash
python source_eval_v6.py --works-cited sources.txt --intended-use B --no-llm
```

**Generate CSV output:**
```bash
# Run evaluation first, then use the JSON to create CSV
python source_eval_v6.py --works-cited sources.txt --intended-use B --out-json results.json
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "ANTHROPIC_API_KEY not set" | Run `export ANTHROPIC_API_KEY="your-key"` |
| "No module named 'requests'" | Run `pip install requests` |
| "No module named 'anthropic'" | Run `pip install anthropic` |
| Many "Manual retrieval needed" | Normal for paywalled sources |
| Evaluation seems slow | LLM calls take time; use `--no-llm` for speed |

---

## Output Files

**report.md** - Human-readable summary:
- Source-by-source breakdown
- Use permission with explanation
- Evidence quotes supporting determination

**report.json** - Machine-readable audit trail:
- All 10 criteria assessments
- LLM decisions logged
- Full evidence chain

---

## Need Help?

- See `CRITERIA_REFERENCE.md` for detailed criteria documentation
- See `VALIDATION_PROTOCOL.md` for accuracy tracking procedures
- See `CLIENT_BRIEFING.md` for full system overview
