"""The offline evaluation: the real pipeline with fake providers and a scripted model (spec 28.5).

    uv run python -m eval.scripts.run_offline [--database-url URL] [--write-baseline PATH]

Needs a PostgreSQL with the schema migrated (pgvector for dense search, full-text search for the
lexical half). It creates its own user and removes everything it made when it finishes. Exits 2 if
any example failed to run: an offline run must be clean.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from covenantiq.db.migrations import head_revision
from covenantiq.db.session import create_session_factory
from covenantiq.db.uow import UnitOfWork
from eval.baseline import write_baseline
from eval.dataset import DEFAULT_VERSION, load_dataset
from eval.report import markdown_summary, write_report
from eval.runner import make_report, offline_providers, run_pipeline

URL_VARIABLES = ("CIQ_EVAL_DATABASE_URL", "CIQ_TEST_DATABASE_URL", "CIQ_DATABASE_URL")
REPORTS = Path("eval/reports")


def database_url(argument: str | None) -> str:
    if argument:
        return argument
    for name in URL_VARIABLES:
        if value := os.environ.get(name):
            return value
    raise SystemExit(
        "no database: pass --database-url or set CIQ_EVAL_DATABASE_URL "
        "(a migrated PostgreSQL with pgvector; it is only written to under a throwaway user)"
    )


async def check_schema(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            try:
                row = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar()
            except Exception:
                row = None
    finally:
        await engine.dispose()
    if row != head_revision():
        raise SystemExit(
            f"the database is at revision {row}, expected {head_revision()}: "
            "run `covenantiq migrate` first"
        )


async def main_async(args: argparse.Namespace) -> int:
    url = database_url(args.database_url)
    await check_schema(url)
    dataset = load_dataset(args.dataset)
    if args.micro:
        dataset = dataset.subset(chat=3, findings=2, vendors=1)
    engine = create_async_engine(url, poolclass=NullPool)
    session_factory = create_session_factory(engine)
    providers = offline_providers()
    try:
        run = await run_pipeline(
            dataset,
            lambda: UnitOfWork(session_factory),
            providers,
            on_progress=lambda message: print(message, file=sys.stderr),
        )
    finally:
        await engine.dispose()
    report = make_report("offline", dataset, run, providers)
    json_path, _ = write_report(report, Path(args.out))
    print(markdown_summary(report))
    print(f"report: {json_path}", file=sys.stderr)
    if args.write_baseline:
        write_baseline(report, Path(args.write_baseline), args.note)
        print(f"baseline written: {args.write_baseline}", file=sys.stderr)
    return 2 if report["errors"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--dataset", default=DEFAULT_VERSION)
    parser.add_argument("--out", default=str(REPORTS))
    parser.add_argument("--micro", action="store_true", help="3 questions, 2 contracts, 1 vendor")
    parser.add_argument("--write-baseline", metavar="PATH")
    parser.add_argument("--note", default="", help="why the baseline is being (re)written")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
