import re

_WORD = re.compile(r"[a-z0-9]+")


class LexicalOverlapReranker:
    """Deterministic reranker for tests and offline runs: the share of the query's words that
    appear in the document. Says nothing about real ranking quality."""

    model = "fake-lexical"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def rerank(self, query: str, docs: list[str], top_k: int) -> list[tuple[int, float]]:
        self.calls.append((query, len(docs)))
        wanted = set(_WORD.findall(query.lower()))
        scored = [
            (index, len(wanted & set(_WORD.findall(doc.lower()))) / len(wanted) if wanted else 0.0)
            for index, doc in enumerate(docs)
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top_k]

    async def aclose(self) -> None:
        return None
