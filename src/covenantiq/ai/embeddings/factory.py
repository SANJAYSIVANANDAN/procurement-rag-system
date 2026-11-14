from covenantiq.ai.embeddings.base import EmbeddingProvider
from covenantiq.ai.embeddings.fake import HashEmbeddingProvider
from covenantiq.ai.embeddings.openai_compat import OpenAICompatEmbeddingProvider
from covenantiq.ai.embeddings.retry import RetryingEmbeddingProvider
from covenantiq.ai.embeddings.voyage import VoyageEmbeddingProvider
from covenantiq.core.config import SHORTENABLE_EMBEDDING_MODELS, Settings


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """The configured provider, wrapped so transient failures are retried.

    Settings validation has already checked that the keys the chosen provider needs are set and
    that the dimension matches the model.
    """
    provider: EmbeddingProvider
    if settings.embedding_provider == "fake":
        return HashEmbeddingProvider(dim=settings.embedding_dim, model=settings.embedding_model)
    if settings.embedding_provider == "voyage":
        if settings.voyage_api_key is None:
            raise ValueError("VOYAGE_API_KEY is not set")
        provider = VoyageEmbeddingProvider(
            settings.voyage_api_key.get_secret_value(),
            settings.embedding_model,
            settings.embedding_dim,
        )
    else:
        if not (settings.openai_compat_base_url and settings.openai_compat_api_key):
            raise ValueError("the openai_compat settings are incomplete")
        provider = OpenAICompatEmbeddingProvider(
            settings.openai_compat_base_url,
            settings.openai_compat_api_key.get_secret_value(),
            settings.embedding_model,
            settings.embedding_dim,
            request_dimensions=settings.embedding_model in SHORTENABLE_EMBEDDING_MODELS,
        )
    return RetryingEmbeddingProvider(provider)
