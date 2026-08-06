"""Lane C US-006 — Shadow Mode interceptor (read-only observation).

Wraps a canonical router and computes a shadow decision in parallel without
mutating the canonical result. Decisions are appended as JSONL to a daily
log file (rotated on UTC date) for downstream Grafana / discrepancy /
checksum tooling (WS-3).

Flag-gated at caller boundary by ``SHADOW_MODE`` env var; per-user gating
via ``SHADOW_USER_ALLOWLIST`` (comma-separated). Bypass path is zero-cost
beyond two env reads per request.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from parallax.obs.log import get_logger, safe_log_warning
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest

__all__ = [
    "CrosswalkStatus",
    "ArbitrationOutcome",
    "ShadowDecisionLog",
    "ShadowInterceptor",
    "SCHEMA_VERSION",
]

_log = get_logger("parallax.router.shadow")

CrosswalkStatus = Literal["ok", "miss", "conflict", "skipped"]
ArbitrationOutcome = Literal["match", "shadow_only", "canonical_only", "diverge"]

# Pin once shipped — bump only on additive field changes; renames/removals are
# breaking. WS-3 ingestion compares this to refuse unknown shapes.
SCHEMA_VERSION = "1.0"

# Score equality across canonical/shadow stores is FP-sensitive (rerank
# arithmetic ordering, BM25 normalisation differences). 1e-6 relative
# tolerance is tight enough to catch real divergence but loose enough to
# absorb arithmetic drift.
_SCORE_REL_TOL = 1e-6


@dataclasses.dataclass(frozen=True)
class ShadowDecisionLog:
    """Canonical decision-log record for Lane C v0.2.0-beta.

    The 9-field schema is the contract surface for downstream Grafana panels
    and the checksum chain in WS-3 — adding or renaming a field is a breaking
    change. ``schema_version`` exists so WS-3 can refuse unknown shapes when
    this set grows.
    """

    query_type: str
    selected_port: str
    crosswalk_status: CrosswalkStatus
    arbitration_outcome: ArbitrationOutcome
    latency_ms: float
    correlation_id: str
    timestamp: str
    user_id: str
    schema_version: str = SCHEMA_VERSION

    def to_jsonl(self) -> str:
        """One-line JSON with sorted keys so checksum chains stay deterministic."""
        return json.dumps(dataclasses.asdict(self), sort_keys=True)


def _is_enabled(user_id: str) -> bool:
    """True when SHADOW_MODE=true AND user_id is in SHADOW_USER_ALLOWLIST."""
    if os.environ.get("SHADOW_MODE", "false").strip().lower() != "true":
        return False
    raw_allow = os.environ.get("SHADOW_USER_ALLOWLIST", "")
    allow = {u.strip() for u in raw_allow.split(",") if u.strip()}
    return user_id in allow


def _hits_equal(canonical: RetrievalEvidence, shadow: RetrievalEvidence) -> bool:
    """Compare hits on (id, kind) exactly and ``score`` within ±1e-6 relative tolerance.

    Body fields can legitimately diverge across crosswalk seeds. Raw float
    ``!=`` on score would mark legitimate matches as ``diverge`` because
    canonical and shadow stores FP-drift on rerank arithmetic — that would
    pollute the very discrepancy signal WS-3 is built to detect.
    """
    if len(canonical.hits) != len(shadow.hits):
        return False
    for c_hit, s_hit in zip(canonical.hits, shadow.hits, strict=True):
        if c_hit.get("id") != s_hit.get("id") or c_hit.get("kind") != s_hit.get("kind"):
            return False
        c_score = c_hit.get("score")
        s_score = s_hit.get("score")
        if c_score is None or s_score is None:
            if c_score != s_score:
                return False
        elif not math.isclose(c_score, s_score, rel_tol=_SCORE_REL_TOL):
            return False
    return True


class ShadowInterceptor:
    """Wrap a canonical router; observe shadow decisions without mutating output.

    Hard invariants (never violated, even on shadow failure):
    - ``query()`` always returns the canonical result
    - shadow exceptions are caught and logged with ``arbitration_outcome="shadow_only"``
    - log writes are fail-silent on OSError
    """

    def __init__(
        self,
        canonical: Any,
        shadow_factory: Callable[[], Any],
    ) -> None:
        self._canonical = canonical
        self._shadow_factory = shadow_factory
        # Resolve and create the log directory ONCE — keeps the mkdir syscall
        # off the hot path. SHADOW_LOG_DIR is read at construction time;
        # per-request env reads are limited to the SHADOW_MODE /
        # SHADOW_USER_ALLOWLIST flag check.
        self._log_dir = Path(os.environ.get("SHADOW_LOG_DIR", "parallax/logs"))
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _today_log_path(self) -> Path:
        """Daily JSONL path for the current UTC day.

        UTC is mandatory: local time would split a session across two daily
        files at TWSE close (CST 14:00 == UTC 06:00) and break the WS-3
        checksum-chain monotonicity guarantee.
        """
        today = time.strftime("%Y-%m-%d", time.gmtime())
        return self._log_dir / f"shadow-decisions-{today}.jsonl"

    def _write_log(self, entry: ShadowDecisionLog) -> None:
        """Append one JSONL line. Fail-silent so user-facing call is never affected."""
        try:
            with self._today_log_path().open("a", encoding="utf-8") as fh:
                fh.write(entry.to_jsonl() + "\n")
        except OSError as exc:
            safe_log_warning(_log, "shadow_log_write_failed", exc=exc)

    def query(
        self,
        request: QueryRequest,
        *,
        correlation_id: str | None = None,
    ) -> RetrievalEvidence:
        """Dispatch the canonical query; observe shadow when flag + allowlist permit."""
        canonical_result = self._canonical.query(request)

        if not _is_enabled(request.user_id):
            return canonical_result

        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())
        start = time.perf_counter()
        outcome: ArbitrationOutcome
        crosswalk: CrosswalkStatus
        try:
            shadow = self._shadow_factory()
            shadow_result = shadow.query(request)
            outcome = "match" if _hits_equal(canonical_result, shadow_result) else "diverge"
            crosswalk = "ok"
        except Exception as exc:  # noqa: BLE001 — shadow failures must never break canonical
            # Structurally the same sink as ``dual_read.py``'s unexpected-exception
            # handler and strictly weaker: this clause is bare ``Exception`` with
            # no unreachable-error carve-out, so EVERY shadow failure lands here,
            # not just the unforeseen ones — and ``shadow`` comes from an injected
            # factory, so the message is not a bounded set. Class + digest only.
            safe_log_warning(_log, "shadow_query_error", exc=exc)
            outcome = "shadow_only"
            crosswalk = "skipped"

        latency_ms = (time.perf_counter() - start) * 1000.0
        timestamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="microseconds")
        self._write_log(
            ShadowDecisionLog(
                query_type=request.query_type.value,
                selected_port="QueryPort",
                crosswalk_status=crosswalk,
                arbitration_outcome=outcome,
                latency_ms=latency_ms,
                correlation_id=cid,
                timestamp=timestamp,
                user_id=request.user_id,
            )
        )
        return canonical_result
