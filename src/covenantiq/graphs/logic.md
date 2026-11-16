# graphs: LangGraph workflows (orchestration only)

## What it is
Three LangGraph state machines that sequence the steps of a task: **chat**, **contract analysis**, **vendor assessment**. Nodes are thin: each reads state, calls one engine or port and returns a partial update. Decisions live in `analysis/` and `rag/`.

*(A fourth, `vendor_comparison/`, compared two or more assessed vendors. It was built in M13 and removed from V1; see `systemdesign.md` §14.)*

## Intuition
A graph is a flowchart with memory. It makes the order of steps, the branches (answer vs. abstain) and the parallel fan-out explicit, and for long jobs it **checkpoints after every step** so a crashed worker resumes instead of restarting and paying for the same LLM calls again.

## The three graphs
**Chat** (`chat/`), no checkpoints (the durable record is the message row):
```
load_context -> classify_intent -+-> respond_direct (greeting etc.)
                                 +-> abstain (no documents)
                                 +-> rewrite_query -> retrieve -> assess_evidence
                                       -> abstain  |  generate_grounded -> validate_citations -> finalize
```
`generate_grounded` streams tokens; it adds a per-request **canary** string to the system prompt, and if the canary appears in the output the reply is replaced by a refusal (the model leaked its instructions). Any node can set `state.error`, and the next edge routes to a safe ending.

**Contract analysis** (`contract_analysis/`), checkpointed:
`load_document_and_rules -> plan_rule_batches -> fan out one assess_rule_batch per batch (Send) -> aggregate -> extract_obligations -> detect_contradictions -> scan_unusual_provisions -> validate_evidence -> build_findings -> persist`. A failed batch degrades only its own rules.

**Vendor assessment** (`vendor_assessment/`), checkpointed:
`load_vendor_scope -> ensure_contract_findings -> map_findings_to_signals -> fan out 6 dimensions -> validate_signal_evidence -> aggregate -> score_deterministic -> explain_dimensions -> persist`.

## Shared files
| File | What it does |
|---|---|
| `runner.py` | Runs the chat graph, validates every event against the SSE models, stamps the message id, publishes, and guarantees **exactly one terminal event** even if the graph crashes. |
| `checkpointed.py` | Start or resume a job's graph: `thread_id = job_id`; an unfinished thread resumes with `astream(None)`; a finished job's checkpoints are deleted. |
| `checkpointing.py` | Creates the LangGraph checkpoint tables in PostgreSQL. |
| `deps.py`, `*/deps.py` | The dependency bundles (engines and ports) each graph receives. |
| `errors.py` | Errors inside a run and the safe user-facing wording. |

## Key ideas
- **Graphs may not import `db`** (import-linter). They reach storage only through ports implemented in `services/` (`ChatPersistencePort`, `AnalysisStorePort`, `VendorStorePort`). Ownership scope travels as an opaque value.
- **State is plain JSON** so it can be checkpointed and resumed exactly.
- **Resume = idempotent**: finished batches are not rerun or re-billed.

## Interview soundbite
"LangGraph gives me explicit control flow, parallel fan-out and PostgreSQL checkpointing. If a worker dies after four of seven rule batches, the redelivered task resumes the same thread and only runs the remaining three."
