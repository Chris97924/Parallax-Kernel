"""Canonical JSON serialization for Apex envelopes + audit rows.

Same rules as ``aphelion-graph/spec/canonical-serialization.md`` Rule 1
(also referenced by ``apex-m5-envelope-spec.md`` §4.1):

  1. Keys lex-sorted ascending (ASCII codepoint order)
  2. No whitespace (no spaces, no newlines)
  3. UTF-8 with NFC normalization on **keys AND string values**
  4. No floats — confidence and other numerics serialize via the
     audit-row schema as strings or ints
  5. Optional fields serialized only when present (never as ``null``)

This module is small enough to live alongside the envelope code rather
than carry a runtime dep on ``aphelion-graph``. Both halves of the wire
(Aphelion package side + Apex envelope side) use the same rules so two
implementations producing "identical-looking JSON" agree on the
sha256 digest.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any


def _nfc(value: Any) -> Any:
    """NFC-normalize keys + string values recursively."""
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", k): _nfc(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_nfc(item) for item in value]
    return value


def canonical_dumps(obj: Any) -> bytes:
    """Serialize ``obj`` as canonical UTF-8 JSON bytes.

    Drops ``None`` values from top-level mappings — optional audit-row
    fields are absent rather than serialized as ``null`` (spec §6.4
    rule 4). Nested dict ``None`` values are preserved (the caller is
    responsible for cleaning).
    """
    cleaned = obj
    if isinstance(obj, dict):
        cleaned = {k: v for k, v in obj.items() if v is not None}
    cleaned = _nfc(cleaned)
    # ``allow_nan=False`` enforces no NaN/Infinity. ``sort_keys=True`` +
    # ``separators=(',', ':')`` enforce keys-sorted + no-whitespace.
    return json.dumps(
        cleaned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(blob: bytes) -> str:
    """SHA-256 hex digest (64 lowercase chars)."""
    return hashlib.sha256(blob).hexdigest()
