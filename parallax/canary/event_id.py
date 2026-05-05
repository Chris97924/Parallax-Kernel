"""US-009.1 §3.1 criterion 1.1 — RFC 4122 UUID v7 generator.

The acceptance spec mandates UUID v7 (NOT a hash-with-timestamp
construction) so that idempotency stays correct even when system clocks
drift more than 5 minutes (criterion 1.4). UUID v7 layout per RFC 9562 §5.7:

::

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                           unix_ts_ms                          |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |          unix_ts_ms           |  ver  |       rand_a          |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |var|                        rand_b                             |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                            rand_b                             |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

Implementation is pure Python because Parallax's minimum is 3.11 and
``uuid.uuid7`` only landed in 3.13.
"""

from __future__ import annotations

import os
import time
import uuid

__all__ = ["uuid7", "is_uuid7"]


def _now_ms() -> int:
    """Return current Unix time in milliseconds. Indirection lets tests freeze it."""
    return time.time_ns() // 1_000_000


def uuid7(*, _ms: int | None = None, _rand: bytes | None = None) -> uuid.UUID:
    """Return a fresh RFC 4122 UUID v7.

    The optional kwargs are private hooks for unit tests — production
    callers should not pass them. ``_ms`` overrides the timestamp,
    ``_rand`` overrides the 10 random bytes (must be exactly 10 bytes).
    """
    ms = _now_ms() if _ms is None else int(_ms)
    if ms < 0 or ms >= (1 << 48):
        raise ValueError(f"unix_ts_ms out of UUID v7 range: {ms}")
    rand = os.urandom(10) if _rand is None else _rand
    if len(rand) != 10:
        raise ValueError(f"_rand must be exactly 10 bytes, got {len(rand)}")

    # Pack 16 bytes: 6 bytes ts + 2 bytes (ver|rand_a) + 8 bytes (var|rand_b)
    ts_bytes = ms.to_bytes(6, "big")
    rand_a = ((rand[0] & 0x0F) << 8) | rand[1]
    ver_rand_a = (0x7 << 12) | rand_a  # version = 7 in high nibble
    rand_b_high = (rand[2] & 0x3F) | 0x80  # variant = 0b10
    body = bytes(
        [
            ts_bytes[0],
            ts_bytes[1],
            ts_bytes[2],
            ts_bytes[3],
            ts_bytes[4],
            ts_bytes[5],
            (ver_rand_a >> 8) & 0xFF,
            ver_rand_a & 0xFF,
            rand_b_high,
            rand[3],
            rand[4],
            rand[5],
            rand[6],
            rand[7],
            rand[8],
            rand[9],
        ]
    )
    return uuid.UUID(bytes=body)


def is_uuid7(value: str | uuid.UUID) -> bool:
    """Return True iff ``value`` is a valid UUID v7 (version=7, variant=10x).

    Accepts both string and :class:`uuid.UUID`. False for malformed input
    rather than raising — callers checking arbitrary input strings should
    not have to wrap each call in try/except.
    """
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            return False
    elif isinstance(value, uuid.UUID):
        parsed = value
    else:
        return False
    if parsed.version != 7:
        return False
    # Variant bits (bits 64-65 of the 128-bit value) must be 0b10
    variant_byte = parsed.bytes[8]
    return (variant_byte & 0xC0) == 0x80
