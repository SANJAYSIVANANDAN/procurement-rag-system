import hashlib
import math
from collections.abc import Sequence


class HashEmbeddingProvider:
    """Deterministic embeddings for tests and offline runs.

    Each word is hashed to a few coordinates, so texts that share words have a high cosine
    similarity. That is enough to exercise retrieval code without a network call; it says
    nothing about real retrieval quality.
    """

    def __init__(self, dim: int = 1024, model: str = "fake-hash") -> None:
        self.dim = dim
        self.model = model
        self.calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in text.lower().split():
            digest = hashlib.sha256(word.encode()).digest()
            for offset in (0, 4, 8):
                index = int.from_bytes(digest[offset : offset + 4], "big") % self.dim
                vector[index] += 1.0 if digest[offset + 12] % 2 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    async def aclose(self) -> None:
        return None
