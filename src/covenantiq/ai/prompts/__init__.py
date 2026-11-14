"""Versioned prompt templates. A prompt is a file named `<name>.v<N>.md`; changing its meaning
means a new version, so an evaluation run can name exactly which prompt produced a result."""

from functools import cache
from importlib import resources


@cache
def load_prompt(name: str) -> str:
    """The text of `<name>.md` from this package (for example `conversation_summary.v1`)."""
    return resources.files(__package__).joinpath(f"{name}.md").read_text(encoding="utf-8").strip()
