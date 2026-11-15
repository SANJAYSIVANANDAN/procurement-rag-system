# ai: model providers behind ports (LLM, embeddings, reranker)

## What it is
Adapters for every external model: the chat/analysis LLM, the embedding model and the reranker, plus the Pydantic schemas the LLM must answer in.

## Intuition
**Ports and adapters.** The rest of the system talks to a small interface (`LLMProvider`, `EmbeddingProvider`, `Reranker`). Which vendor, model and effort sits behind it is configuration. That is why switching from Anthropic to OpenAI was an env change, and why every test runs against a fake with no network.

## Files
### `llm/`
| File | What it does |
|---|---|
| `base.py` | The port: `stream(request)`, `structured(request, schema)`, `count_tokens`. Typed errors (`LLMUnavailable`, `LLMRefused`, `LLMTruncated`, `LLMBadRequest`). |
| `routes.py` | Loads `config/llm_routes.yaml`: each **route** (`answer`, `classify`, `rewrite`, `summarize`, `analysis`...) has its own model, effort, max tokens and timeout. Cheap routes can use small models. |
| `anthropic_provider.py` | Anthropic SDK adapter. Checks `stop_reason` (refusal, max_tokens) before trusting content. |
| `openai_provider.py` | OpenAI Chat Completions via `httpx`. Uses `max_completion_tokens`, strict `json_schema` structured output (with fallback), checks `finish_reason`. |
| `retry.py` | `RetryingLLMProvider`: retries transient errors twice, **only before the first token** (retrying mid-stream would duplicate text). |
| `fake.py` | Deterministic fake for tests and the offline evaluation. |
| `factory.py` | Picks the adapter from `CIQ_LLM_PROVIDER` (`anthropic`, `openai`, `fake`). |

### `embeddings/`
`base.py` (port: `embed_documents`, `embed_query`), `openai_compat.py` (OpenAI-format servers), `voyage.py`, `fake.py`, `retry.py` (backoff on 429/5xx), `factory.py`. Dimension is fixed (1024) and must match the pgvector column.

### `rerank/`
| File | What it does |
|---|---|
| `base.py` | Port: score a query against candidate passages. |
| `cohere.py` | Hosted reranker. |
| `server.py` + `remote.py` | A **separate container** holds the cross-encoder model (PyTorch). The API and workers call it over HTTP via `remote.py`, so heavy CPU work never blocks an event loop. |
| `breaker.py` | Circuit breaker: 3 failures opens it for 30 s, then retries. |
| `fake.py`, `factory.py` | Test fake; chooses `none`, `cohere`, `cross_encoder` (the reranker container, via `remote.py`) or `fake`. |

### `schemas/`
`analysis.py`, `vendor.py`, `chat.py`: what the model must return. **No numeric scores anywhere**: the model returns categories (status, confidence level) and quotes; scores and rankings are computed in code.

## Key ideas
- **Errors never carry prompt text or keys**, since prompts contain contract text.
- **Structured output is validated by Pydantic** even when the API enforces a schema.
- **Optional reranking**: off, slow or failing, retrieval keeps the fused order and the evidence gate becomes stricter.

## Interview soundbite
"The model is a replaceable detail behind a port. I route by task, so cheap classification and expensive grounded answers can use different models, and the LLM only classifies and explains. Numbers are never its job."
