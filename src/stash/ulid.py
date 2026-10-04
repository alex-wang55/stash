"""Time-sortable IDs; used as the sort key so date filters are key-range queries, not scans."""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
_TIME_LEN = 10
_RAND_LEN = 16


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))


def new(now_ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ms, _TIME_LEN) + _encode(rand, _RAND_LEN)


def lower_bound(ms: int) -> str:
    return _encode(ms, _TIME_LEN) + "0" * _RAND_LEN


def upper_bound(ms: int) -> str:
    return _encode(ms, _TIME_LEN) + "Z" * _RAND_LEN


def timestamp_ms(ulid: str) -> int:
    value = 0
    for ch in ulid[:_TIME_LEN]:
        value = value * 32 + _INDEX[ch]
    return value


def is_valid(value: str) -> bool:
    return len(value) == 26 and all(c in _INDEX for c in value) and value[0] <= "7"
