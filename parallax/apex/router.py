"""Apex M7 — Apex public-read router.

Implements ``docs/m7-prep/apex-router-public-read-spec.md`` v0.1.3-reframe
(#67 spec + #68 exception-class reconcile, merged on ``main-next``). M7 scope
is **public read only**: the router reads claim/evidence from Aphelion-packaged
``.aphelion.tar`` files (M6 ingest route A), runs R4 detection, and exposes a
typed retrieve surface to the retrieve facade. The private read/write surface
lives in Perihelion (M6.5) and is **never** touched here (§3 boundaries).

Design (KISS/DRY): this module is a thin composition layer.

  * A package-reading **claim loader** walks ``PARALLAX_APHELION_PACKAGE_DIR``
    and runs the spec §3.3 ordered Aphelion call set per package
    (``unpack`` → ``verify_package`` → ``validate_signatures`` → projection),
    mapping every Aphelion exception onto a typed
    :class:`~parallax.router.aphelion_adapter.AphelionUnreachableError` reason
    (§4.3 table).
  * The existing M5 :class:`~parallax.router.aphelion_adapter.AphelionReadAdapter`
    is reused for R4 detection (§3.3 call #4) + the audit-row write-order
    invariant + the audit-write atomic-abort path (§4.3 audit-write row). M7
    does not re-implement those; it injects the package loader and surfaces the
    §4.5 observability metrics around the call.

Boundaries honored here (normative, §3):
  * §3.1/§3.2 — no Perihelion import, no Perihelion schema read.
  * §3.3 — read path uses only the Aphelion public surface; it does NOT re-run
    the ingest-time signer-manifest trust-store gate (that enforcement happens
    at ingest, before a package enters the corpus, not on read).
  * §3.4 — private read/write stays in Perihelion's in-process API.

Spec anchors: §3.3 (read-path call set), §4.3 (failure modes), §4.4 (M5
P-A2' startup validation carry-forward), §4.5 (observability metrics).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import prometheus_client
from aphelion.errors import SchemaError, SecurityError, SemanticError, VerificationError
from aphelion.signer import SignerVerificationError
from aphelion.unpacker import unpack
from aphelion.validator import validate_signatures
from aphelion.verifier import verify_package
from aphelion.yaml_canonical import parse_frontmatter, split_frontmatter

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.aphelion_adapter import (
    CLAIM_CONTENT_KEY,
    AphelionReadAdapter,
    AphelionUnreachableError,
    AuditConnProvider,
)
from parallax.router.contracts import QueryRequest

__all__ = [
    "REQUIRED_APHELION_MIN_VERSION",
    "ApexPublicReadRouter",
    "ApexRouterStartupError",
    "assert_aphelion_version",
    "classify_package_exception",
    "validate_package_dir",
]

_log = logging.getLogger(__name__)

# SD-1: spec §3.3 tentatively suggested "likely 0.5.0", but the installed +
# verified Aphelion lib surface (unpacker.unpack / verifier.verify_package /
# validator.validate_signatures / read_adapter.AphelionReadAdapter) is fully
# present at 0.4.0. The floor is pinned to the actual verified version; bump
# it in a follow-up once Aphelion v0.5 is deployed. See PR body §SD.
REQUIRED_APHELION_MIN_VERSION = "0.4.0"


# ---------------------------------------------------------------------------
# Observability metrics (§4.5) — idempotent registration so module re-import
# (test reload) does not trip prometheus_client's duplicate-name ValueError.
# ---------------------------------------------------------------------------


def _get_or_create(factory: Any, base_name: str) -> Any:
    """Return a freshly-created collector, or the already-registered one.

    prometheus_client raises ``ValueError`` when a metric name is registered
    twice. On collision we look the existing collector back up by matching the
    base name (or any of its exposed ``_total`` / ``_bucket`` / ``_count``
    children) in the default REGISTRY. Mirrors the convention in
    :mod:`parallax.router.inflight`.
    """
    try:
        return factory()
    except ValueError:
        registry = prometheus_client.REGISTRY._names_to_collectors  # type: ignore[attr-defined]
        # Match by the collector's constructor name (``_name``), NOT a prefix
        # scan over exposed sample names — ``parallax_apex_read`` is a prefix of
        # ``parallax_apex_read_latency_ms`` etc., so a startswith match could
        # return the wrong collector (e.g. the latency Histogram for the read
        # Counter) after a module reload.
        for collector in set(registry.values()):
            if getattr(collector, "_name", None) == base_name:
                return collector
        raise


# Histogram buckets are in MILLISECONDS (the metric observes ``elapsed_ms``).
# prometheus_client's DEFAULT_BUCKETS top out at a finite ``le=10.0`` — fine for
# second-scale latencies, but here a 50ms read lands only in ``+Inf`` and
# ``histogram_quantile`` saturates at 10, making the §4.2 p99<100ms SLA alert
# structurally unmeasurable. §4.5 left "bucket set TBD by impl PR"; this
# observability slice pins it. Boundaries straddle the 100ms SLA so p50/p90/p99
# resolve below it and breaches above it stay visible.
READ_LATENCY_BUCKETS = (1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 75.0, 100.0, 150.0, 250.0, 500.0, 1000.0)

READ_LATENCY = _get_or_create(
    lambda: prometheus_client.Histogram(
        "parallax_apex_read_latency_ms",
        "Apex public-read wall-clock latency in milliseconds.",
        ["result"],
        buckets=READ_LATENCY_BUCKETS,
    ),
    "parallax_apex_read_latency_ms",
)

READ_TOTAL = _get_or_create(
    lambda: prometheus_client.Counter(
        "parallax_apex_read",
        "Apex public-read calls by outcome.",
        ["result"],
    ),
    "parallax_apex_read",
)

READ_ERRORS = _get_or_create(
    lambda: prometheus_client.Counter(
        "parallax_apex_read_errors",
        "Apex public-read AphelionUnreachableError raises by reason.",
        ["reason", "exc_class"],
    ),
    "parallax_apex_read_errors",
)

PACKAGE_DIR_ERRORS = _get_or_create(
    lambda: prometheus_client.Counter(
        "parallax_apex_package_dir_errors",
        "PARALLAX_APHELION_PACKAGE_DIR misconfiguration detections.",
        ["reason"],
    ),
    "parallax_apex_package_dir_errors",
)

EMPTY_RESULT = _get_or_create(
    lambda: prometheus_client.Counter(
        "parallax_apex_empty_result",
        "Apex public-read calls returning [] for non-error reasons.",
        ["cause"],
    ),
    "parallax_apex_empty_result",
)

EMPTY_CORPUS = _get_or_create(
    lambda: prometheus_client.Counter(
        "parallax_apex_empty_corpus",
        "First-read-per-process detection of an accessible-but-empty corpus.",
    ),
    "parallax_apex_empty_corpus",
)

AUDIT_WRITE_FAILURES = _get_or_create(
    lambda: prometheus_client.Counter(
        "parallax_apex_audit_write_failures",
        "Read-path audit-row write failures (distinct from M5 write-path counter).",
        ["cause"],
    ),
    "parallax_apex_audit_write_failures",
)

LIB_VERSION_INFO = _get_or_create(
    lambda: prometheus_client.Gauge(
        "parallax_apex_lib_version_info",
        "Aphelion lib version pinned vs required (info-style, value=1).",
        ["version", "min_version"],
    ),
    "parallax_apex_lib_version_info",
)


# ---------------------------------------------------------------------------
# Startup version assertion (§3.3)
# ---------------------------------------------------------------------------


class ApexRouterStartupError(RuntimeError):
    """Raised when the router refuses to come up (version / config gate)."""


def _parse_version(value: str) -> tuple[int, ...]:
    """Best-effort numeric version tuple (ignores pre-release suffixes)."""
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _installed_aphelion_version() -> str:
    import aphelion

    return getattr(aphelion, "__version__", "0.0.0")


def assert_aphelion_version(min_version: str = REQUIRED_APHELION_MIN_VERSION) -> str:
    """Assert the installed Aphelion lib meets ``min_version`` (§3.3).

    Returns the installed version string on success and emits the
    ``parallax_apex_lib_version_info`` gauge. Raises
    :class:`ApexRouterStartupError` when the floor is not met — the router
    refuses to serve rather than risk silent behavioral divergence.
    """
    installed = _installed_aphelion_version()
    if _parse_version(installed) < _parse_version(min_version):
        raise ApexRouterStartupError(
            f"aphelion {installed} is below required floor {min_version}; "
            "refusing to start the Apex public-read router"
        )
    LIB_VERSION_INFO.labels(version=installed, min_version=min_version).set(1)
    return installed


# ---------------------------------------------------------------------------
# Package directory validation (§4.4 — M5 §3.1c P-A2' carry-forward)
# ---------------------------------------------------------------------------


def validate_package_dir(package_dir: Path | str) -> Path:
    """Validate ``PARALLAX_APHELION_PACKAGE_DIR`` per M5 P-A2' (§4.4).

    Requirements: absolute path, no ``..`` segments, exists, is a directory,
    readable. On any failure the granular
    ``parallax_apex_package_dir_errors_total{reason}`` counter increments and
    :class:`AphelionUnreachableError` ``reason="package_dir_inaccessible"`` is
    raised — never a silent degrade to empty result (§4.3).
    """
    path = Path(package_dir)

    def _fail(reason: str) -> AphelionUnreachableError:
        PACKAGE_DIR_ERRORS.labels(reason=reason).inc()
        _log.error("package_dir invalid (%s): %s", reason, path)
        return AphelionUnreachableError("package_dir_inaccessible")

    if not path.is_absolute():
        raise _fail("relative_path")
    if any(part == ".." for part in path.parts):
        raise _fail("traversal_segment")
    # A dangling symlink reports exists()==False; classify it as the spec-named
    # ``broken_symlink`` reason (§4.5) rather than folding it into dir_missing.
    if path.is_symlink() and not path.exists():
        raise _fail("broken_symlink")
    if not path.exists():
        raise _fail("dir_missing")
    if not path.is_dir():
        raise _fail("dir_missing")
    if not os.access(path, os.R_OK):
        raise _fail("perm_denied")
    return path.resolve()


# ---------------------------------------------------------------------------
# Aphelion exception → §4.3 reason classification (pure, fully unit-tested)
# ---------------------------------------------------------------------------


def classify_package_exception(exc: BaseException) -> str:
    """Map an Aphelion read-path exception to a §4.3 ``reason`` tag.

    ``FileNotFoundError`` is checked before the generic OSError surface;
    ``SignerVerificationError`` branches on its machine-readable ``code``
    (``E_SIGNER_REQUIRED`` → unsigned, anything else → untrusted signer chain).
    Unknown exceptions fall through to ``lib_error`` so the read path never
    leaks an unclassified failure to the caller.
    """
    if isinstance(exc, FileNotFoundError):
        return "package_missing"
    if isinstance(exc, SignerVerificationError):
        if getattr(exc, "code", None) == "E_SIGNER_REQUIRED":
            return "unsigned_package"
        return "signer_untrusted"
    if isinstance(exc, (SecurityError, VerificationError, SemanticError, SchemaError)):
        return "package_corrupt"
    return "lib_error"


# ---------------------------------------------------------------------------
# Apex public-read router
# ---------------------------------------------------------------------------


class ApexPublicReadRouter:
    """In-process Apex public-read router (spec §8.1 Q1 recommendation).

    Composes the package-reading claim loader with the M5
    :class:`AphelionReadAdapter`. ``query`` runs the full §3.3 read path and
    emits the §4.5 metric set; failures surface as typed
    :class:`AphelionUnreachableError` (§4.3) — never a silent fall-through to
    Perihelion (§3.2 boundary).

    Args:
        package_dir: ``PARALLAX_APHELION_PACKAGE_DIR``; validated at construction
            time per §4.4 (absolute, exists, readable, no ``..`` segments).
        audit_conn_provider: zero-arg callable returning the calling thread's
            audit-db connection; forwarded unchanged to the M5 adapter so the
            audit write-order invariant + atomic-abort path apply unchanged.
        timeout_ms: reserved dual-read timeout, forwarded to the M5 adapter.

    Raises:
        ApexRouterStartupError: installed Aphelion below the version floor.
        AphelionUnreachableError: ``package_dir`` failed validation.
    """

    def __init__(
        self,
        *,
        package_dir: Path | str,
        audit_conn_provider: AuditConnProvider,
        timeout_ms: float = 100.0,
    ) -> None:
        assert_aphelion_version()
        self._package_dir = validate_package_dir(package_dir)
        self._last_corpus_empty = False
        self._adapter = AphelionReadAdapter(
            audit_conn_provider=audit_conn_provider,
            package_dir=self._package_dir,
            timeout_ms=timeout_ms,
            claim_loader=self._load_candidate_claims,
        )

    @property
    def last_envelope(self) -> Any:
        """Expose the M5 adapter's last envelope (None after an atomic abort)."""
        return self._adapter.last_envelope

    # -- claim loader (§3.3 read-path call set) -----------------------------

    def _load_candidate_claims(self, _request: QueryRequest) -> tuple[Mapping[str, Any], ...]:
        """Walk the package dir and project verified claims (§3.3).

        Raises :class:`AphelionUnreachableError` (typed reason) on any package
        verification failure. An accessible-but-empty corpus is a legitimate
        fresh-deploy state: it returns ``()`` and emits ``empty_corpus``.

        Concurrency: a router instance is single-context per call — it relies on
        the M5 adapter's per-call ``audit_conn_provider`` (thread-local) pattern,
        and ``_last_corpus_empty`` is per-instance scratch state set here and read
        by :meth:`query` immediately after. Do not share one instance across
        concurrent queries without external synchronization.
        """
        tars = sorted(self._package_dir.glob("*.aphelion.tar"))
        self._last_corpus_empty = not tars
        if not tars:
            EMPTY_CORPUS.inc()
            return ()

        claims: list[Mapping[str, Any]] = []
        for tar in tars:
            # Abort-on-first-bad-package is load-bearing (§4.3): any exception
            # from _read_package_claims MUST propagate and fail the whole read.
            # Silently skipping a corrupt package and returning partial results
            # from the others is forbidden — a future "graceful degradation"
            # mode would be an explicit spec deviation, not a quiet loop change.
            claims.extend(self._read_package_claims(tar))
        return tuple(claims)

    def _read_package_claims(self, tar: Path) -> tuple[Mapping[str, Any], ...]:
        """Run the §3.3 ordered Aphelion call set for one package.

        Order: ``unpack`` (local FS, v0.2 §S2.5 safety) → ``verify_package``
        (package signer chain, ``require_signed=True``) → ``validate_signatures``
        (claim-level sigs) → projection. Any Aphelion exception is remapped to a
        typed :class:`AphelionUnreachableError` per §4.3.
        """
        try:
            with tempfile.TemporaryDirectory(prefix="apex_read_") as tmp_dir:
                extracted = unpack(tar, tmp_dir)
                verify_package(tar, require_signed=True, require_notary=False)
                validate_signatures(tar)
                return self._project_claims(tar, Path(extracted))
        except AphelionUnreachableError:
            raise
        except Exception as exc:  # noqa: BLE001 — total fence, classified below
            reason = classify_package_exception(exc)
            _log.error(
                "apex public-read package failure (%s) for %s: %s",
                reason,
                tar.name,
                exc,
                exc_info=True,
            )
            raise AphelionUnreachableError(reason) from exc

    @staticmethod
    def _project_claims(tar: Path, extracted: Path) -> tuple[Mapping[str, Any], ...]:
        """Read each manifest claim's frontmatter; inject ``package_id``.

        ``package_id`` is projected from the manifest into each claim mapping so
        the M5 adapter's audit-row write path (which reads ``package_id`` off the
        primary claim) functions on real packages. The merge is immutable (new
        dict per claim) per the project's immutability rule.

        Defense-in-depth (W6 cross-review Tier-A): the manifest's ``path`` field
        is attacker-controllable, so each claim path is resolved and asserted to
        stay within the extraction dir before it is read off the local
        filesystem. A ``..``-escaping or absolute path is treated as a corrupt
        package and rejected — even though ``verify_package`` (which ran first)
        would normally reject a divergent fileset, the read path does not rely on
        that to keep its own filesystem access in-bounds.
        """
        # A package that passed verify_package but whose manifest is unreadable /
        # not JSON / missing required keys is a corrupt package, not a lib bug —
        # surface package_corrupt for a precise metric label (rather than letting
        # it fall through the caller's total fence into the vaguer lib_error).
        try:
            manifest = json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))
            package_id = manifest["package_id"]
            entries = manifest["claims"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            _log.error("apex public-read manifest unreadable in %s: %s", tar.name, exc)
            raise AphelionUnreachableError("package_corrupt") from exc

        extracted_root = extracted.resolve()
        out: list[Mapping[str, Any]] = []
        for entry in entries:
            claim_path = (extracted / entry["path"]).resolve()
            try:
                claim_path.relative_to(extracted_root)
            except ValueError as exc:
                _log.error(
                    "apex public-read rejected traversal claim path %r in %s",
                    entry.get("path"),
                    tar.name,
                )
                raise AphelionUnreachableError("package_corrupt") from exc
            try:
                frontmatter, body = _read_claim_frontmatter(claim_path)
            except (OSError, UnicodeDecodeError) as exc:
                # Claim file present-but-unreadable (e.g. perms changed after
                # unpack) is distinct from a genuine lib bug; tag it corrupt.
                _log.error(
                    "apex public-read claim unreadable %s in %s: %s",
                    entry.get("path"),
                    tar.name,
                    exc,
                )
                raise AphelionUnreachableError("package_corrupt") from exc
            # Project package_id (for the audit row) + the markdown body (for
            # content-bearing hits, #71). The merge is immutable (new dict per
            # claim) per the project's immutability rule.
            out.append({**frontmatter, "package_id": package_id, CLAIM_CONTENT_KEY: body})
        return tuple(out)

    # -- public read --------------------------------------------------------

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        """Run the Apex public read and emit §4.5 metrics.

        Returns :class:`RetrievalEvidence` (possibly with empty ``hits`` — a
        legitimate "no public knowledge" result). Raises
        :class:`AphelionUnreachableError` on any failure (§4.3); the caller
        (retrieve facade) MUST NOT substitute Perihelion content for the failed
        or empty Apex slot (§3.2 / §8.3 Q3 no-substitution rule).
        """
        start = time.perf_counter()
        try:
            evidence = self._adapter.query(request)
        except AphelionUnreachableError as err:
            self._record(start, "error")
            self._record_error(err)
            raise
        except Exception as exc:
            # §4.3/§4.5: an unexpected (non-typed) adapter failure must still be
            # metric-visible — making read failures observable is this layer's
            # whole job, and a metric-dark error would let ApexReadErrorRateHigh
            # silently never fire. Record the call as an error + tag it lib_error
            # with the real class, then re-raise the ORIGINAL exception unchanged
            # (the caller's contract is preserved; the type is not masked).
            self._record(start, "error")
            READ_ERRORS.labels(reason="lib_error", exc_class=type(exc).__name__).inc()
            raise

        self._record(start, "success")
        if not evidence.hits:
            cause = "empty_corpus" if self._last_corpus_empty else "no_matching_claim"
            EMPTY_RESULT.labels(cause=cause).inc()
        return evidence

    @staticmethod
    def _record(start: float, result: str) -> None:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        READ_TOTAL.labels(result=result).inc()
        READ_LATENCY.labels(result=result).observe(elapsed_ms)

    @staticmethod
    def _record_error(err: AphelionUnreachableError) -> None:
        reason = err.reason
        cause_cls = type(err.__cause__).__name__ if err.__cause__ is not None else ""
        exc_class = cause_cls if reason == "lib_error" else ""
        READ_ERRORS.labels(reason=reason, exc_class=exc_class).inc()
        if reason == "audit_db_write_failed":
            AUDIT_WRITE_FAILURES.labels(cause=cause_cls or "unknown").inc()


def _read_claim_frontmatter(claim_path: Path) -> tuple[Mapping[str, Any], str]:
    """Parse a v0.3 markdown claim into ``(frontmatter, body)`` (mirrors ingest).

    The markdown body carries the claim's content. #71 surfaces it as the
    content-bearing hit text downstream, so it is returned alongside the
    frontmatter rather than discarded.
    """
    text = claim_path.read_text(encoding="utf-8")
    yaml_part, body = split_frontmatter(text)
    data, _key_order = parse_frontmatter(yaml_part)
    return data, body
