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
from dataclasses import replace
from pathlib import Path
from typing import Any

import prometheus_client
from aphelion.errors import SchemaError, SecurityError, SemanticError, VerificationError
from aphelion.read_adapter import ConflictClass
from aphelion.signer import SignerVerificationError
from aphelion.unpacker import unpack
from aphelion.validator import validate_signatures
from aphelion.verifier import verify_package
from aphelion.yaml_canonical import parse_frontmatter, split_frontmatter

from parallax.apex import subject_index
from parallax.apex.resolver import DEFAULT_TOP_K, resolve_subjects
from parallax.obs.subsystem_readiness import SUBSYSTEM_APEX_M7, mark_wired
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
    "ZERO_EXPORT_LABEL_SETS",
    "ApexPublicReadRouter",
    "ApexRouterStartupError",
    "assert_aphelion_version",
    "classify_package_exception",
    "prime_zero_series",
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
        "Number of reads that hit an empty Apex corpus (incremented on every such read).",
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
# Zero-export priming (#106.2)
# ---------------------------------------------------------------------------
# Registering a collector is not exposition, and for a LABELLED collector it is
# not even a series: prometheus_client creates a child only when ``.labels()``
# is called, so an untouched Counter contributes a HELP/TYPE header and nothing
# under it. Prometheus records no samples, and every rule in
# prometheus/rules/parallax-apex-m7.rules.yml evaluates against no-data — which
# is silent in exactly the way a healthy subsystem is silent. That is the whole
# #106 defect class.
#
# So the label sets below are materialised at server startup. Each is 0.0 and
# stays 0.0 until M7 read is wired into the server, at which point the real
# increments take over.
#
# WHICH LABEL VALUES, AND WHY THESE: one rule, applied mechanically.
#
#     Prime exactly the label sets the shipped consumers select, leaving every
#     label they do not constrain EMPTY. A collector no consumer label-selects
#     gets a single all-empty series.
#
# An empty label value is a legitimate Prometheus series (it is equivalent to
# the label being absent for matching purposes) and it is already part of this
# metric's vocabulary — ``_record_error`` writes ``exc_class=""`` on every
# non-``lib_error`` path. The alternative, priming a plausible-looking value
# like ``reason="package_missing"``, would invent dimensionality: it asserts
# that a specific failure mode was checked and found clean, which is precisely
# the false reassurance the zero-export was accused of. An empty label says "no
# value has ever been observed", which is true.
#
# The one exception is ``cause="empty_corpus"``, and it is an exception because
# ApexStuckEmptyCorpus selects that exact label — priming anything else would
# leave that alert matching nothing, i.e. still dead.
#
# THE ZEROS ARE ONLY HALF THE SIGNAL. parallax_subsystem_wired{subsystem=
# "apex_m7"} reports 0.0 until ApexPublicReadRouter is constructed, so a reader
# can tell a real zero from a subsystem that never ran. The rule annotations
# name it.
#
# KNOWN RESIDUAL — READ BEFORE WIRING M7. Priming one label set does not give
# every FUTURE label set a zero to step from. When M7 runs and the first
# `reason="package_missing"` error occurs, that child series is created already
# at 1.0; increase() over samples that are all 1.0 is 0, and the alerts here
# aggregate with sum(), so each per-series increase is 0 and the total is too.
# The first occurrence of each new label value is therefore invisible to these
# rules; the second onwards are fine.
#
# Not fixed here, and deliberately: the fix is to prime the label space, and
# this one is not closed — `exc_class` is an exception class name and
# `audit_write_failures.cause` is `type(exc).__name__`, so there is no
# enumeration to prime. (Contrast parallax/canary/exporter.py, whose stages and
# outcomes ARE closed and spec-pinned, so it primes the full grid and does not
# have this hole.) The options when M7 lands are to enumerate the reasons that
# can be enumerated, or to move these alerts off `increase() > 0` onto the
# resulting level. Both are decisions for the PR that wires M7 — until then the
# subsystem does not run and the residual cannot fire.
ZERO_EXPORT_LABEL_SETS: dict[str, tuple[dict[str, str], ...]] = {
    "parallax_apex_read": ({"result": ""},),
    "parallax_apex_read_latency_ms": ({"result": ""},),
    "parallax_apex_read_errors": ({"reason": "", "exc_class": ""},),
    "parallax_apex_package_dir_errors": ({"reason": ""},),
    # ApexStuckEmptyCorpus selects cause="empty_corpus" by equality.
    "parallax_apex_empty_result": ({"cause": "empty_corpus"},),
    "parallax_apex_empty_corpus": ({},),
    "parallax_apex_audit_write_failures": ({"cause": ""},),
    "parallax_apex_lib_version_info": ({"version": "", "min_version": ""},),
}

_ZERO_EXPORT_COLLECTORS: dict[str, Any] = {
    "parallax_apex_read": READ_TOTAL,
    "parallax_apex_read_latency_ms": READ_LATENCY,
    "parallax_apex_read_errors": READ_ERRORS,
    "parallax_apex_package_dir_errors": PACKAGE_DIR_ERRORS,
    "parallax_apex_empty_result": EMPTY_RESULT,
    "parallax_apex_empty_corpus": EMPTY_CORPUS,
    "parallax_apex_audit_write_failures": AUDIT_WRITE_FAILURES,
    "parallax_apex_lib_version_info": LIB_VERSION_INFO,
}


def prime_zero_series() -> None:
    """Materialise every apex M7 series at zero. Idempotent.

    ``.labels(...)`` creates the child and leaves it at its zero value; it is
    NOT an increment, so calling this twice (or after real traffic has started)
    changes nothing. The unlabelled ``EMPTY_CORPUS`` counter already has its
    single series from construction and is listed only so the table below stays
    the exhaustive statement of what this module zero-exports.
    """
    for name, label_sets in ZERO_EXPORT_LABEL_SETS.items():
        collector = _ZERO_EXPORT_COLLECTORS[name]
        for labels in label_sets:
            if labels:
                collector.labels(**labels)


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
# Free-text read helpers (#71 Part B)
# ---------------------------------------------------------------------------


def _explicit_subject(request: QueryRequest) -> str | None:
    """The caller-supplied exact subject (``params['subject']``), or ``None``.

    Mirrors the precedence in ``aphelion_adapter._resolve_subject``: an explicit
    ``params['subject']`` selects the exact-subject path; its absence means the
    read is free-text and must go through the resolver against ``request.q``.
    """
    if request.params:
        explicit = request.params.get("subject")
        if isinstance(explicit, str) and explicit:
            return explicit
    return None


def _with_subject(request: QueryRequest, subject: str) -> QueryRequest:
    """Return a copy of ``request`` with ``params['subject']`` set (immutable).

    Used to drive the per-candidate exact-subject R4 read from a resolved
    free-text query without mutating the caller's request.
    """
    params = dict(request.params or {})
    params["subject"] = subject
    return replace(request, params=params)


#: The miss marker, built from the same enum member the M5 adapter renders
#: (``parallax/router/aphelion_adapter.py`` builds its note from
#: ``result.conflict_class.value``). Spelled from :class:`ConflictClass` rather
#: than as a literal because the two must be byte-identical: an exact-subject
#: miss reaches a consumer through the adapter and a free-text miss through
#: :func:`_empty_evidence`, and a hand-written ``NOT_FOUND`` here would hand out
#: two spellings for one condition from one class.
_NOT_FOUND_NOTE = f"conflict_class={ConflictClass.NOT_FOUND.value}"


def _empty_evidence(extra_notes: tuple[str, ...] = ()) -> RetrievalEvidence:
    """A content-free :class:`RetrievalEvidence` for the free-text empty paths.

    Mirrors the adapter's miss note (``conflict_class=not_found``) so a free-text
    empty result is shaped like an exact-subject miss for downstream consumers.
    """
    return RetrievalEvidence(
        hits=(),
        stages=("aphelion_v03_r4",),
        notes=(_NOT_FOUND_NOTE, *extra_notes),
    )


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
        resolver_top_k: max candidate subjects the free-text resolver admits
            per query (#71 Part B, design doc D4). Ignored on the exact-subject
            path (``params["subject"]`` set).

    Raises:
        ApexRouterStartupError: installed Aphelion below the version floor.
        AphelionUnreachableError: ``package_dir`` failed validation.

    Read modes:
        * **Exact subject** — ``request.params["subject"]`` is set: the caller
          already resolved the subject, so the read scans the full corpus and
          runs exact-subject R4 (the pre-#71 behaviour, unchanged).
        * **Free-text** — no explicit subject: ``request.q`` is resolved to
          candidate subjects via the M6-maintained subject index +
          token-overlap (#71 Part B), and R4 runs per candidate subject over
          only that subject's package(s); hits are merged.
    """

    def __init__(
        self,
        *,
        package_dir: Path | str,
        audit_conn_provider: AuditConnProvider,
        timeout_ms: float = 100.0,
        resolver_top_k: int = DEFAULT_TOP_K,
    ) -> None:
        assert_aphelion_version()
        self._package_dir = validate_package_dir(package_dir)
        self._last_corpus_empty = False
        self._resolver_top_k = resolver_top_k
        # Per-query scratch (#71 Part B): the set of package basenames the next
        # claim-loader call should unpack. ``None`` means "scan the full corpus"
        # — the exact-subject / pre-#71 behaviour. The free-text path sets it to
        # one candidate subject's packages before each per-subject R4 read. Like
        # ``_last_corpus_empty`` this is single-context-per-call state; do not
        # share one instance across concurrent queries without synchronization.
        self._scoped_packages: tuple[str, ...] | None = None
        # #106.2 — from here on the zero-exported apex series are real
        # measurements rather than placeholders, and the readiness gauge is what
        # says so. Marked in the constructor, not at a wiring site, so it cannot
        # claim the subsystem is live when nothing built it.
        mark_wired(SUBSYSTEM_APEX_M7)
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

        Scope (#71 Part B): when :attr:`_scoped_packages` is ``None`` the full
        corpus is scanned (exact-subject / pre-#71 behaviour). When it is set
        (free-text per-subject read), only those package basenames are unpacked
        — the §8.4 win: a directory listing is cheap, but the expensive
        unpack+verify (:meth:`_read_package_claims`) runs only on the resolved
        package(s), not every ``.aphelion.tar``. The scoped names are
        intersected with the live glob so a stale index entry can never make the
        loader unpack a vanished file.

        Concurrency: a router instance is single-context per call — it relies on
        the M5 adapter's per-call ``audit_conn_provider`` (thread-local) pattern,
        and ``_last_corpus_empty`` / ``_scoped_packages`` are per-instance scratch
        state set by :meth:`query` and read here. Do not share one instance
        across concurrent queries without external synchronization.
        """
        all_tars = sorted(self._package_dir.glob("*.aphelion.tar"))
        # Emptiness is a property of the whole corpus, not of a scoped subset, so
        # the empty_corpus signal stays accurate on the per-subject free-text path.
        self._last_corpus_empty = not all_tars
        if not all_tars:
            EMPTY_CORPUS.inc()
            return ()

        if self._scoped_packages is None:
            tars = all_tars
        else:
            scoped = set(self._scoped_packages)
            tars = [tar for tar in all_tars if tar.name in scoped]
            if not tars:
                # Subject resolved to no on-disk package (corpus non-empty): a
                # genuine no-match, surfaced as empty_result by query(), not
                # empty_corpus.
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

        Dispatches on the read mode (see the class docstring): an explicit
        ``params["subject"]`` takes the exact-subject path; otherwise the
        free-text resolver path runs. Both share this method's metric +
        error-classification envelope so no read is ever metric-dark.

        Returns :class:`RetrievalEvidence` (possibly with empty ``hits`` — a
        legitimate "no public knowledge" result). Raises
        :class:`AphelionUnreachableError` on any failure (§4.3); the caller
        (retrieve facade) MUST NOT substitute Perihelion content for the failed
        or empty Apex slot (§3.2 / §8.3 Q3 no-substitution rule).
        """
        start = time.perf_counter()
        # Reset per-query scratch so a free-text early-return cannot leave a
        # stale scope for the next query on a reused instance.
        self._scoped_packages = None
        try:
            if _explicit_subject(request) is not None:
                evidence = self._query_exact(request)
            else:
                evidence = self._query_freetext(request)
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

    def _query_exact(self, request: QueryRequest) -> RetrievalEvidence:
        """Exact-subject read (pre-#71 path, unchanged behaviour).

        The caller named the subject via ``params["subject"]``, so no resolver
        or index is involved: the loader scans the full corpus
        (``_scoped_packages is None``) and the M5 adapter runs exact-subject R4
        exactly as before. Used by existing callers and M5 dual-read.
        """
        self._scoped_packages = None
        return self._adapter.query(request)

    def _query_freetext(self, request: QueryRequest) -> RetrievalEvidence:
        """Free-text read: resolve prompt → candidate subjects → per-subject R4 → merge.

        Steps (#71 Part B, design doc §3.1):
          1. ``subject_index.load_or_rebuild`` returns a freshness-guaranteed
             index (rebuild-on-stale; never silent-stale per §8.4).
          2. ``resolve_subjects`` (token-overlap, D2/D3) maps ``request.q`` to
             ranked candidate subjects against the index's subject labels.
          3. For each candidate, the loader is scoped to that subject's
             package(s) and the M5 adapter runs unchanged exact-subject R4 — so
             R4 detection, the audit write-order invariant, and the atomic-abort
             path are reused verbatim (not re-implemented).
          4. Hits are merged across subjects, deduped by claim id, ordered by
             candidate rank (D4).

        Negative-result contract (§4.5): an empty corpus, no resolved candidate,
        or candidates whose R4 yields nothing all return empty ``hits`` — never a
        fabricated hit — and ``query`` fires
        ``parallax_apex_empty_result{cause=...}`` accordingly.
        """
        index = subject_index.load_or_rebuild(self._package_dir, self._scan_index_entries)
        self._last_corpus_empty = index.is_empty()
        if self._last_corpus_empty:
            EMPTY_CORPUS.inc()
            return _empty_evidence(("corpus=empty",))

        candidates = resolve_subjects(
            request.q or "", index.subjects(), top_k=self._resolver_top_k
        )
        if not candidates:
            # Genuine miss: the prompt shares no meaningful token with any known
            # subject. Surface empty (query() → empty_result{no_matching_claim}).
            return _empty_evidence(("resolver=token_overlap", "candidates=0"))

        merged: list[dict] = []
        seen: set[str] = set()
        for candidate in candidates:
            self._scoped_packages = index.packages_for_subject(candidate.subject)
            if not self._scoped_packages:
                continue
            evidence = self._adapter.query(_with_subject(request, candidate.subject))
            for hit in evidence.hits:
                hid = str(hit.get("id") or "")
                if hid and hid in seen:
                    continue
                if hid:
                    seen.add(hid)
                merged.append(hit)

        notes = (
            "resolver=token_overlap",
            f"candidates={len(candidates)}",
            f"subjects={','.join(c.subject for c in candidates)}",
        )
        if not merged:
            # Third negative path, and it used to be the odd one out. An empty
            # corpus and an unresolved prompt both return through
            # ``_empty_evidence``, which stamps ``conflict_class=not_found``;
            # candidates that resolve to real packages whose R4 reads yield
            # nothing is the same kind of miss and has to carry the same marker,
            # or a consumer keying on it recognises two of the three and
            # silently mis-classifies the third. The resolver notes are passed
            # through, so routing here costs no context.
            return _empty_evidence(notes)
        return RetrievalEvidence(hits=tuple(merged), stages=("aphelion_v03_r4",), notes=notes)

    def _scan_index_entries(self) -> list[subject_index.IndexEntry]:
        """Full-corpus scan that rebuilds the subject index (read-path fallback).

        Reuses the same safe unpack + verify + projection as the read path
        (:meth:`_read_package_claims`), so the index only ever points at
        packages a read would accept, and a corrupt package aborts the scan with
        the same typed :class:`AphelionUnreachableError` a read would raise
        (§4.3 abort-on-bad-package). This is the O(all packages) fallback that
        only fires on a cache miss — steady-state reads use the M6-maintained
        index and never reach it (§8.4 ceiling note).
        """
        entries: list[subject_index.IndexEntry] = []
        for tar in sorted(self._package_dir.glob("*.aphelion.tar")):
            for claim in self._read_package_claims(tar):
                subject = claim.get("subject")
                claim_id = claim.get("claim_id")
                if isinstance(subject, str) and subject and isinstance(claim_id, str):
                    entries.append(
                        subject_index.IndexEntry(
                            subject=subject,
                            package_id=str(claim.get("package_id") or ""),
                            claim_id=claim_id,
                            package_file=tar.name,
                        )
                    )
        return entries

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
