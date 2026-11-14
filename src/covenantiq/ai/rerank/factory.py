from covenantiq.ai.rerank.base import Reranker
from covenantiq.ai.rerank.cohere import CohereReranker
from covenantiq.ai.rerank.fake import LexicalOverlapReranker
from covenantiq.ai.rerank.remote import RemoteReranker
from covenantiq.core.config import Settings


def build_reranker(settings: Settings) -> Reranker | None:
    """The configured reranker, or None when reranking is switched off (retrieval then keeps
    the fused order, spec 19.8)."""
    if settings.rerank_provider == "none":
        return None
    if settings.rerank_provider == "fake":
        return LexicalOverlapReranker()
    if settings.rerank_provider == "cohere":
        if settings.cohere_api_key is None:
            raise ValueError("COHERE_API_KEY is not set")
        return CohereReranker(
            settings.cohere_api_key.get_secret_value(),
            model=settings.rerank_model,
            timeout_seconds=settings.rerank_timeout_seconds,
            max_documents=settings.rerank_max_documents,
        )
    if settings.rerank_provider == "cross_encoder":
        return RemoteReranker(
            settings.rerank_url,
            model=settings.rerank_model,
            timeout_seconds=settings.rerank_timeout_seconds,
            max_documents=settings.rerank_max_documents,
        )
    raise NotImplementedError(
        "the Voyage reranker is not implemented yet; use CIQ_RERANK_PROVIDER=cohere, "
        "cross_encoder, fake or none"
    )
