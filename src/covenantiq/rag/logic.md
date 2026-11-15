# rag: retrieval and grounding (the heart of chat)

## What it is
Everything between "a question" and "a trustworthy, cited answer", **without calling a model** except for generation itself: query keywords, hybrid retrieval, fusion, reranking, evidence gate, prompt context, citation validation, abstention and caches. Tunables live in `config/rag.yaml`.

## Intuition
An LLM will happily answer from imagination. This package makes it answer *only from retrieved text*, proves each claim against its source, and refuses when the evidence is thin. Think of it as: **find, judge, cite, verify, or decline**.

## Flow
```
query -> keywords (rule-based, no LLM)
      -> embed query (cached 24 h)
      -> dense top 30  +  lexical top 30   (run concurrently)
      -> RRF fusion (k = 60; x1.1 boost for the wanted clause type)
      -> rerank top 30 (3 s timeout, optional, scores cached 1 h) -> keep 8
      -> expand to parent clauses
      -> evidence gate: strong / moderate / weak / none
      -> weak or none: abstain with a template (no model call)
      -> context builder: numbered <source id="C1" ...> blocks
      -> model answers with [C#] markers
      -> citation validator (deterministic) -> shown / shown with notice / abstain
```

## Files
| File | What it does |
|---|---|
| `retrieval.py` | `RetrievalEngine.retrieve`: the single place contracts are searched. Chat, contract analysis and vendor assessment all call it with different parameters. |
| `query_analysis.py` | Content words joined with `or` plus quoted phrases, for `websearch_to_tsquery`. Legal terms need exact matching, which is why lexical search sits next to vectors. |
| `fusion.py` | Reciprocal Rank Fusion: `sum(w / (60 + rank))`. Rank-based because cosine similarity and full-text rank are on different scales. |
| `evidence.py` | The gate. Thresholds (from `rag.yaml`): top rerank score >= 0.55 strong, >= 0.35 moderate, a single chunk >= 0.70 is enough breadth, at least 50% of key terms present. Without a reranker the gate falls back to "top result is in both lists' top 10" and is stricter. |
| `context.py` | Builds `<source>` blocks and escapes a literal `<source`/`</source` in the text so a document cannot close the tag. Tells the model sources are untrusted data. |
| `citations.py` | Splits the answer into sentences; every `[C#]` must be a real source; each cited sentence must be supported by its source (word overlap, quote fuzzy match, numbers must appear); uncited sentences with figures are flagged. Fully offline. |
| `abstention.py` | The fixed "the documents don't cover this" reply. |
| `cache.py` | Query-embedding and rerank-score caches in Redis. Never makes a request slower: short timeouts, any failure = miss, three failures skips the cache for 30 s. Retrieval *results* are deliberately never cached. |
| `config.py`, `types.py`, `factory.py`, `session.py` | Config loader, data shapes, wiring, DB access. |

## Key ideas
- **Hybrid search**: vectors catch meaning, full-text catches exact terms ("Schedule 3", defined terms).
- **Abstain rather than guess**: a template reply costs no tokens and cannot hallucinate.
- **Verification is code, not another LLM**, so it is cheap, deterministic and testable.
- **Honest limitation**: thresholds are initial defaults, not calibrated against real-model scores.

## Interview soundbite
"Retrieval is hybrid and fused by rank, then gated: if the evidence is weak I refuse without calling the model. After generation, a deterministic validator checks every citation and number against the source text."
