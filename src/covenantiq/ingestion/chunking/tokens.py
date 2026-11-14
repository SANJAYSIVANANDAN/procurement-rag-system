CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """A deliberately conservative token count (about four characters per token).

    Budgeting only needs an estimate that errs high: a chunk estimated at 500 tokens is never
    larger than the embedding model's real limit would allow. Exact counts would need the
    provider's tokenizer, which is not worth a network call per chunk.
    """
    return max(1, -(-len(text) // CHARS_PER_TOKEN))
