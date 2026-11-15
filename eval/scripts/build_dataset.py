"""Ingest the evaluation documents and export their chunk ids (spec 28.4 `build_dataset.py`).

    uv run python -m eval.scripts.build_dataset [--database-url URL] [--out chunks.json]

A helper for looking at what the real chunker makes of the dataset's contracts: for each document,
its clauses and the ids, pages and first words of its chunks. It uses the offline providers and
removes what it created when it finishes.
"""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from covenantiq.db.session import create_session_factory
from covenantiq.db.uow import UnitOfWork
from covenantiq.rag.config import RagConfig
from eval import pipeline
from eval.dataset import DEFAULT_VERSION, load_dataset
from eval.runner import offline_providers
from eval.scripts.run_offline import check_schema, database_url


async def export(url: str, version: str) -> dict[str, Any]:
    dataset = load_dataset(version)
    engine = create_async_engine(url, poolclass=NullPool)
    factory = create_session_factory(engine)
    providers = offline_providers()
    try:
        with tempfile.TemporaryDirectory(prefix="ciq-eval-") as scratch:
            world = await pipeline.ingest(
                dataset,
                lambda: UnitOfWork(factory),
                providers.embedder,
                providers.reranker,
                Path(scratch),
                RagConfig(),
            )
            try:
                async with UnitOfWork(factory) as uow:
                    rows = (
                        await uow.session.execute(
                            text(
                                "SELECT d.title, c.ordinal, c.id, c.page_start, "
                                "left(c.text, 60), k.clause_ref "
                                "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
                                "JOIN document_clauses k ON k.id = c.clause_id "
                                "WHERE d.owner_id = :o ORDER BY d.title, c.ordinal"
                            ),
                            {"o": world.owner_id},
                        )
                    ).all()
            finally:
                await pipeline.cleanup(world)
    finally:
        await engine.dispose()
    out: dict[str, Any] = {"dataset": version, "documents": {}}
    for title, ordinal, chunk_id, page, start, ref in rows:
        out["documents"].setdefault(title, []).append(
            {
                "ordinal": ordinal,
                "chunk_id": str(chunk_id),
                "page": page,
                "clause_ref": ref,
                "starts": start,
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--dataset", default=DEFAULT_VERSION)
    parser.add_argument("--out")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    url = database_url(args.database_url)
    asyncio.run(check_schema(url))
    result = asyncio.run(export(url, args.dataset))
    payload = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"{sum(len(v) for v in result['documents'].values())} chunks written to {args.out}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
