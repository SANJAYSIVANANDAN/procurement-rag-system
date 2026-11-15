# api: the HTTP layer (FastAPI)

## What it is
The thin outside of the system: routes, request/response schemas, middleware, error mapping, dependency wiring and two background loops. It translates HTTP into service calls and back. It contains **no business logic**.

## Intuition
A request enters, middleware tags it, a dependency identifies the user and rate-limits, a router calls exactly one service method, and an exception handler turns any domain error into the standard error envelope. That is all the API does.

## Files
| File | What it does |
|---|---|
| `main.py` | `create_app()`: builds the app, middleware, routers under `/api/v1`, and the **lifespan**: builds the container, then starts two background tasks (stale-message sweeper every 60 s; stuck-job sweeper) and declares the broker topology. Closes everything on shutdown. |
| `container.py` | The **composition root**. Builds engine, Redis, Celery publisher, LLM, embeddings, reranker, retrieval, memory, publisher and every service, and exposes them as one `Container`. The only place concrete classes are chosen. |
| `deps.py` | FastAPI dependencies: `get_container`, `get_current_principal` (reads the Bearer token, returns the user), `rate_limit(bucket)`. |
| `middleware.py` | `RequestContextMiddleware`: accepts or generates `X-Request-ID`, binds it to logs, adds security headers. |
| `limits.py` | `BodySizeLimitMiddleware`: rejects oversized uploads while streaming (413), before the whole file is read. |
| `errors.py` | Handlers for domain errors, validation errors, HTTP errors and unexpected errors. One JSON shape: `{error: {code, message, details, request_id}}`. Unexpected errors never leak internals. |
| `background.py` | `declare_broker_topology` and `sweep_loop`. |
| `routers/` | `auth`, `documents`, `analysis`, `jobs`, `conversations`, `chat` (including the SSE stream), `vendors`, `vendor_comparisons`, `health` (live/ready). |
| `schemas/` | Pydantic request and response models, separate from DB models so the wire format can evolve independently. |

## Request lifecycle
```
request -> RequestContextMiddleware (request id) -> BodySizeLimit
        -> router dependency: get_current_principal (JWT) + rate_limit (Redis)
        -> service method (ownership checked in PostgreSQL first)
        -> response schema   |  CovenantError -> handler -> error envelope
```

## Key ideas
- **Dependency injection through one container**: tests build their own container with fakes; production builds it from settings.
- **Ownership returns 404, never 403**, so a caller cannot learn that someone else's id exists. A matrix test hits every endpoint with user B and A's ids.
- **Heavy work is not done here.** Uploads, analysis and assessments return `202` with a job id; a Celery worker does the work. Chat is the exception: it runs as an asyncio task inside the API process and streams over SSE.
- **Two uvicorn workers**, each with its own event loop, container and background tasks.

## Interview soundbite
"Routers are one-liners: authenticate, rate-limit, call a service. Everything interesting is in services, which makes the API trivially testable with an ASGI client and a fake container."
