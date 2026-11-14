"""Every Redis key the application uses, built in one place (spec 23.3).

All keys start with `ciq:{env}:`, so a Redis shared between environments never mixes them. No other
module writes a key string: a test fails if one does. Session keys embed the user id, so two users
can never address the same session, and the ownership check is implicit in the key.

Celery results are not here: they live in Redis database 1 and never share a keyspace with these.
"""

from uuid import UUID


def _prefix(env: str) -> str:
    return f"ciq:{env}:"


# ---- conversation sessions (DB 0) ----------------------------------------------------------


def session_window(env: str, user_id: UUID, conversation_id: UUID) -> str:
    """LIST of JSON turns, oldest first."""
    return f"{_prefix(env)}sess:{user_id}:{conversation_id}:window"


def session_meta(env: str, user_id: UUID, conversation_id: UUID) -> str:
    """HASH: last_seq, summary, summary_through_seq, window_tokens, schema_v."""
    return f"{_prefix(env)}sess:{user_id}:{conversation_id}:meta"


def stream(env: str, message_id: UUID) -> str:
    """STREAM of server-sent events for one assistant message."""
    return f"{_prefix(env)}stream:{message_id}"


def conversation_lock(env: str, conversation_id: UUID) -> str:
    """The producer token held while a reply is being generated."""
    return f"{_prefix(env)}lock:conv:{conversation_id}"


# ---- rate limits and locks -----------------------------------------------------------------


def rate_limit(env: str, bucket: str, identifier: str, window_index: int) -> str:
    return f"{_prefix(env)}rl:{bucket}:{identifier}:{window_index}"


def provider_rate_limit(env: str, provider: str) -> str:
    return f"{_prefix(env)}rl:provider:{provider}"


def sweep_lock(env: str) -> str:
    return f"{_prefix(env)}lock:sweep"


def sweep_redispatched(env: str, job_id: UUID) -> str:
    return f"{_prefix(env)}sweep:redispatched:{job_id}"


# ---- caches --------------------------------------------------------------------------------


def query_embedding(env: str, model: str, query_sha: str) -> str:
    return f"{_prefix(env)}cache:qemb:{model}:{query_sha}"


def rerank_score(env: str, model: str, query_sha: str, chunk_id: UUID) -> str:
    return f"{_prefix(env)}cache:rr:{model}:{query_sha}:{chunk_id}"
