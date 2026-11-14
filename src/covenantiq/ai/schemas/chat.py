"""What the model returns for the two small structured calls in a chat turn."""

from typing import Annotated

from pydantic import BaseModel, Field

from covenantiq.core.enums import QueryIntent


class IntentClassification(BaseModel):
    intent: QueryIntent
    is_follow_up: bool = False  # the question only makes sense with the earlier conversation


class StandaloneQuery(BaseModel):
    """The question rewritten so it can be searched without the conversation."""

    query: Annotated[str, Field(min_length=1, max_length=1000)]
