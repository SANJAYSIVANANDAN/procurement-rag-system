# Offline evaluation, dataset 1.0.0: the baseline

Measured on 2026-11-14 by `python -m eval.scripts.run_offline`, on a real PostgreSQL 16 with
pgvector. The numbers below are copied from the run that produced `eval/baselines/offline_v1.0.0.json`;
a second run reproduced every value exactly. Nothing here is a target or an estimate.

**What this run is, and is not.** It drives the real ingestion, chunking, hybrid retrieval, evidence
gate, chat graph, citation validator, contract analysis graph (heuristics, status logic, evidence
validation) and vendor scoring. It does this with *fake providers*: hash embeddings, a lexical
reranker and a scripted model. So it measures the deterministic logic and says **nothing about answer
quality, retrieval quality with real embeddings, or any real model**. The full run (real providers
and RAGAS's LLM-judged metrics) has not been made: no provider keys were available. There is no
full report in this directory, and no number here may be quoted as one.

The scripted model: for a question that has an answer it writes the labelled answer and cites the
retrieved sources that contain the labelled passage; for an unanswerable question it plays the worst
case, a confident claim nothing supports, so the evidence gate and citation validator are tested
rather than a well-behaved model. For rule assessment it answers from the labels, but only when the
labelled clause is among the passages it was shown.

Dataset: 12 synthetic contracts (written for this project; no third-party text), 73 chat questions
(58 answerable, 15 not; 6 follow-ups with history, 4 across two documents), complete findings labels
for every applicable rule of each contract, 5 vendor cases.

| Metric | Value | 95% CI | n |
|---|---|---|---|
| Retrieval hit@1 | 0.603 | 0.466 to 0.724 | 58 |
| Retrieval hit@3 | 0.828 | 0.724 to 0.914 | 58 |
| Retrieval hit@5 | 0.931 | 0.845 to 0.983 | 58 |
| MRR | 0.732 | 0.640 to 0.816 | 58 |
| Context precision (non-LLM) | 0.265 | 0.237 to 0.291 | 58 |
| Context recall (non-LLM) | 1.000 | 1.000 to 1.000 | 58 |
| Citation correctness | 0.981 | 0.940 to 1.000 | 49 |
| Citation validity rate | 1.000 | 1.000 to 1.000 | 49 |
| Unsupported claim rate (lower is better) | 0.019 | 0.000 to 0.060 | 49 |
| Abstention accuracy | 0.795 | 0.699 to 0.877 | 73 |
| False abstention rate (lower is better) | 0.155 | 0.069 to 0.259 | 58 |
| False answer rate (lower is better) | 0.400 | 0.133 to 0.667 | 15 |
| Evidence gate stopped an unanswerable question before the model | 0.600 | 0.333 to 0.867 | 15 |
| Clause extraction accuracy | 1.000 | 1.000 to 1.000 | 12 |
| Risk classification precision | 1.000 | 1.000 to 1.000 | 9 |
| Risk classification recall | 1.000 | 1.000 to 1.000 | 9 |
| Finding status accuracy (exact) | 1.000 | 1.000 to 1.000 | 12 |
| Not-expected findings that appeared (lower is better) | 0.000 | 0.000 to 0.000 | 12 |
| Vendor signal agreement | 1.000 | 1.000 to 1.000 | 5 |
| Vendor dimension order claims | 1.000 | 1.000 to 1.000 | 2 |
| Vendor scoring determinism | 1.000 | 1.000 to 1.000 | 5 |

## How to read it

- **The perfect scores are not achievements.** Findings, risk classification and vendor agreement are
  1.000 because the scripted oracle answers correctly when retrieval surfaces the labelled clause, and
  these metrics then check that the deterministic logic (heuristics, status rules, evidence checks,
  aggregation, scoring) turns correct inputs into the correct statuses. That is a regression test of
  the logic. With a real model they will be lower.
- **What the weaker numbers do show** (they depend on the fake embeddings and fake reranker, so they
  are *not* estimates for production):
  - Retrieval puts the labelled passage first in 60% of questions and within the top five in 93%.
  - The evidence gate wrongly refuses 15.5% of answerable questions (9 of 58): fee, renewal and
    follow-up questions, where the lexical reranker scores a short clause below the 0.35 threshold.
  - It lets 40% of unanswerable questions through to the model (6 of 15). In every such case the
    citation validator then marked the fabricated claim `ungrounded` and appended its notice, so the
    user is warned, but the answer is still shown. Whether that should abstain instead is a product
    decision, recorded in progress.md.
- **The evidence thresholds were not calibrated.** Spec 19.10's thresholds (0.55 / 0.35 / 0.70) are
  still the specified defaults. Calibrating them needs a real embedding model and reranker, which this
  run did not have, and fake scores say nothing about where real ones fall.
- **Intervals are wide where n is small** (15 unanswerable questions, 12 contracts, 5 vendors).
  Differences smaller than the interval are not evidence of anything.

## What the gate does and does not catch

`config/eval_tolerances.yaml` allows a worse-direction move of 0.02 per metric (0.0 for vendor scoring
determinism). Deliberate regressions checked against this baseline:

| Change | Result |
|---|---|
| Return one passage instead of eight (`k_final=1`) | caught: hit@3 0.828 to 0.603, hit@5 0.931 to 0.603, MRR 0.732 to 0.603, abstention accuracy 0.795 to 0.630 |
| Evidence thresholds set to 1.0 (refuse everything) | caught: abstention accuracy 0.795 to 0.205, false abstention rate 0.155 to 1.000 |
| **RRF constant raised from 60 to 1,000** | **not caught: no metric moves.** The reranker reorders the fused candidates, so the constant only affects which candidates enter the set. |

The last row contradicts the example in spec 28's acceptance criteria. If the RRF constant matters in
production it will matter with a real reranker, or with reranking switched off, and only a run with
those providers can show it.
