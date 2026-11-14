import base64
import binascii
import json

from covenantiq.core.errors import ValidationFailed


def encode_cursor(*parts: str) -> str:
    """An opaque keyset cursor: the sort-key values of the last row a client has seen."""
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, *, parts: int) -> list[str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailed("The cursor is invalid.") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != parts
        or not all(isinstance(part, str) for part in decoded)
    ):
        raise ValidationFailed("The cursor is invalid.")
    return decoded
