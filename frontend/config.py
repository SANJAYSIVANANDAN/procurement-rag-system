"""The one setting the UI has: where the API is. It knows nothing else (spec 27.4)."""

import os

DEFAULT_API_BASE_URL = "http://localhost:8000/api/v1"


def api_base_url() -> str:
    return os.environ.get("CIQ_API_BASE_URL", DEFAULT_API_BASE_URL)
