import secrets
import threading
import time
from uuid import UUID

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> UUID:
    """Time-ordered UUID (RFC 9562 v7); strictly increasing within one process."""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _counter = secrets.randbits(11)  # top bit clear leaves headroom for increments
        else:
            ms = _last_ms
            _counter += 1
            if _counter > 0xFFF:
                ms += 1
                _counter = secrets.randbits(11)
        _last_ms = ms
        counter = _counter

    value = (ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return UUID(int=value)
