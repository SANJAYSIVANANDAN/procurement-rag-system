# db: PostgreSQL access (models, repositories, unit of work)

## What it is
Everything that talks to PostgreSQL: SQLAlchemy models, repositories (one per aggregate), the ownership scope and the unit of work. PostgreSQL is the **source of truth** for the whole system, and Redis is only a cache and stream buffer.

## Intuition
Two rules shape this package:
1. **Every query is scoped to the owner.** There is no "get document by id", only "get document by id *for this user*". Forgetting a tenant filter is made impossible by the type system, not by discipline.
2. **Services own transactions, repositories don't.** A repository runs statements; a service decides what is committed together.

## Files
| Path | What it does |
|---|---|
| `base.py` | Declarative `Base`, a constraint naming convention (so Alembic migrations are stable), `TimestampMixin` (`created_at`/`updated_at`), `OwnedMixin` (adds `owner_id`), helper for PostgreSQL enums. |
| `session.py` | Builds the async engine (asyncpg) and session factory. `null_pool=True` is used by Celery children. |
| `scope.py` | `OwnershipScope(owner_id)`. `scope.apply(statement, Model)` adds `WHERE owner_id = ...` to a query. |
| `uow.py` | `UnitOfWork`: opens a session, hands out repositories, `commit()`/`rollback()`. Used as `async with uow_factory() as uow`. |
| `migrations.py` | Runs Alembic upgrades (used by `covenantiq migrate`). Migrations themselves are in `db/migrations/versions`. |
| `models/` | Tables: `user`, `document`, `chunk` (child, has the embedding), `clause` (parent), `conversation`, `message`, `citation`, `job` (processing and analysis jobs), `analysis` (findings), `vendor`, `vendor_risk` (assessments, comparisons). |
| `repositories/base.py` | `Repository` base; `ensure_owned`; `cas_transition` (compare-and-set status update: `UPDATE ... WHERE status = expected` and check the row count). |
| `repositories/chunk_search.py` | The two retrieval queries: **dense** (pgvector cosine over an HNSW index) and **lexical** (full-text search, `english` tsvector, `websearch_to_tsquery`). Both filter on owner, allowed documents, and "document is ready and not deleted". |
| `repositories/*.py` | One repository per aggregate: documents, chunks, clauses, conversations, messages, citations, findings, assessments, comparisons, vendors, users, processing jobs, analysis jobs. |

## Key ideas
- **Compare-and-set (CAS)**: job claiming and stage transitions use `UPDATE ... WHERE status IN (...)`. Whoever updates a row wins, so two workers never run the same job and no lock server is needed.
- **Parent/child storage**: `clause` is the readable unit (what we show and cite), `chunk` is the small embedded unit (what we search). Retrieval finds chunks and returns their parent clauses.
- **Redundant safety filter**: search joins to the document and re-checks `ready` and not deleted, even though the caller should already pass only those.
- **A contract test** (`tests/unit/db/test_scope_contract.py`) fails if any user-data repository method lacks a scope parameter.

## Interview soundbite
"Multi-tenancy is enforced at the repository boundary: every query takes an `OwnershipScope`, and a test checks it. State transitions are compare-and-set updates, which gives me idempotent, race-free job claiming with plain PostgreSQL."
