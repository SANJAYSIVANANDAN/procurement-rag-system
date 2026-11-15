# core: the shared foundation

## What it is
The small, dependency-free layer every other package imports. It contains no business logic: settings, enums, errors, IDs, logging, security primitives and the Redis key builder.

## Intuition
If two packages both need the same fact (what a job status is, what error code the API returns, what a Redis key looks like), it lives here exactly once. Everything else is allowed to depend on `core`, and `core` depends on nothing in the project.

## Files
| File | What it does |
|---|---|
| `config.py` | One pydantic `Settings` class. Reads `CIQ_*` environment variables (plus `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`). Validates at start-up, for example it refuses to start if the chosen provider has no key. |
| `enums.py` | Every closed vocabulary: `DocumentStatus`, `JobStatus`, `ProcessingStage`, `ContractType`, `MessageStatus`, `GroundingStatus`, `ClauseStatus`, `Severity`, `RiskDimension` and others. They map to PostgreSQL enums (`DB_ENUMS`). |
| `errors.py` | `ErrorCode` plus the `CovenantError` hierarchy (`NotFound`, `Conflict`, `RateLimited`, `GenerationInProgress`...). The API turns each into an HTTP status and one JSON error shape. `TransientError` means "retry"; `PermanentTaskError` means "dead-letter". |
| `ids.py` | `uuid7()`: time-ordered UUIDs, so primary keys sort by creation and indexes stay compact. |
| `logging.py` | structlog set-up: JSON in production, console in development. Redacts secrets and never prints local variables in tracebacks (`show_locals=False`), so contract text cannot leak through an exception. |
| `redis_keys.py` | The only place a Redis key string is built. All keys start `ciq:{env}:`. Session keys embed the user id, so ownership is part of the key. A test fails if any other module writes a key literal. |
| `security.py` | Argon2 password hashing (with rehash on login), password policy (12 to 128 chars, common-password list), JWT access tokens (HS256, 30 s clock-skew leeway). |
| `task_names.py` | Celery task names shared by the publisher (services) and the consumer (workers), so the two can never drift. |

## Why it is shaped this way
- **Central error vocabulary**: services raise domain errors and never think about HTTP. The API layer is the only place that maps code to status.
- **Typed enums as DB enums**: a typo in a status is a type error, not a silent bad row.
- **Fail at start-up**: a bad setting stops the process immediately instead of surfacing mid-request.

## Interview soundbite
"`core` is the leaf of the dependency graph: settings, enums, the error taxonomy, logging and Redis key naming. Because everything imports it and it imports nothing, I can enforce layering with import-linter."
