# analysis: pure decision engines for contracts and vendors

## What it is
All the *judgement* of the system, written as **pure code**: no model calls, no database, no clock. Two sub-packages: `contract/` (is each clause present, risky, absent?) and `vendor/` (turn findings into a risk score and compare vendors).

## Intuition
The LLM is good at reading text and bad at being consistent. So it only **proposes** (a category, a quote, a confidence word). This package **decides**: statuses, severities, scores and rankings come from deterministic rules. Same input always gives the same output, and every number can be explained in a test.

## `contract/`
| File | What it does |
|---|---|
| `rules_schema.py`, `rules_loader.py` | The YAML ruleset (in `config/rules/`): each rule has a risk category, retrieval queries, what a good clause looks like, red-flag patterns, severities per contract type. Invalid rules fail at start-up. |
| `heuristics.py` | Regex red flags over retrieved passages. They can only push *toward* risk; absence of a red flag never makes a clause "acceptable". |
| `status.py` | Decides `present / partial / absent / insufficient_evidence`, plus severity and confidence. **Rule: absence is a claim that needs evidence of search.** A clause is `absent` only if the text was fully extracted, searches found nothing close, no tagged clause matches, and no pattern hints at it. Otherwise it is honestly `insufficient_evidence`. |
| `findings.py` | Builds findings. Absence and could-not-assess statements are written from facts, not by the model. Each finding carries its evidence. |
| `contradictions.py` | Finds candidate pairs deterministically (two different notice periods in two places); a model only confirms or dismisses them (a handful per document). |
| `obligations.py` | Checks extracted obligations and deadlines against sources. |
| `types.py` | Plain Pydantic models that survive JSON, because graph state is checkpointed. |

## `vendor/`
| File | What it does |
|---|---|
| `config_schema.py` | Signal taxonomy and weights (`config/vendor/`): 26 signals in 6 dimensions, dimension weights 0.25 / 0.25 / 0.20 / 0.15 / 0.10 / 0.05. Validated at load. |
| `signals.py` | Maps contract findings to signal observations. |
| `aggregation.py` | Combines a signal across a vendor's documents, conservatively and independent of document order. |
| `scoring.py` | Risk 0 to 100 (higher is riskier). Gaps lower **completeness**, never the score; weights are renormalised over the dimensions actually assessed, and that is disclosed. A favourable signal can never raise a risk score. |
| `comparison.py` | Compares vendors only where **both** have evidence. An absence is never ranked as better or worse than an established clause. A ranking is produced only when every assessment is complete enough, otherwise it is suppressed with a reason. |
| `types.py` | Assessment data models. |

## Key ideas
- **Properties are tested** (Hypothesis): order of documents does not matter; gaps do not change scores; favourable signals never raise risk.
- **Honest uncertainty**: "provisional" assessments, "not comparable" cells, `insufficient_evidence`.
- **Import rule**: this package imports nothing from db, services or api.

## Interview soundbite
"I separated reading from judging. The LLM extracts evidence; deterministic code assigns status, severity and score. That makes the risk output reproducible, auditable and unit-testable, and it prevents the model from inventing numbers."
