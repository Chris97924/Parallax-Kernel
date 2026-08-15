"""Mutation-hardening for ``parallax.apex.router`` (overnight-20260816 S6).

Additive companion to ``test_router.py``. Every test below was written against
a semantic mutant that the existing suite let through.

The gap the existing suite has is that it drives the router through *whole
reads* on well-formed corpora and then asserts on what came back — the claim,
the reason tag, the error counter for that reason. What it does not pin is the
supporting apparatus each read depends on:

  * the ``result="error"`` / ``result="success"`` split on the call counter
    (only the per-reason error counter was ever asserted, so recording a failed
    read as a success was invisible),
  * the *unit* of the latency histogram (declared in milliseconds, with buckets
    straddling the 100ms SLA — nothing observed the number that lands there),
  * the ``empty_result{cause}`` label on the exact-subject path (only the
    free-text path and the raw ``empty_corpus`` counter were asserted),
  * the branches no happy-path read reaches: an unreadable package dir, a
    causeless audit-write failure, a claim id surfacing under two candidate
    subjects, a blank explicit subject.

Version-floor comparison is here for the same reason: both existing cases sit
far from the boundary (installed vs. a 99.0.0 floor), so neither an off-by-one
on the comparison nor a per-digit version split changed anything.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from parallax.apex import router as router_mod
from parallax.apex import subject_index
from parallax.apex.audit_db import open_audit_db
from parallax.apex.router import (
    ApexPublicReadRouter,
    _explicit_subject,
    _parse_version,
    assert_aphelion_version,
    validate_package_dir,
)
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.aphelion_adapter import AphelionUnreachableError
from parallax.router.contracts import QueryRequest
from parallax.router.types import QueryType


def _counter_value(metric: Any, **labels: str) -> float:
    child = metric.labels(**labels) if labels else metric
    return child._value.get()  # type: ignore[attr-defined]


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    d = tmp_path / "packages"
    d.mkdir()
    return d


@pytest.fixture
def audit_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = open_audit_db(tmp_path / "harden_audit.db", validate=False)
    yield conn
    conn.close()


def _make_router(package_dir: Path, audit_conn: sqlite3.Connection) -> ApexPublicReadRouter:
    return ApexPublicReadRouter(package_dir=package_dir, audit_conn_provider=lambda: audit_conn)


def _exact_query(subject: str) -> QueryRequest:
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT,
        user_id="s6-test-user",
        q=subject,
        params={"subject": subject},
    )


def _freetext_query(prompt: str, params: dict[str, Any] | None = None) -> QueryRequest:
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT,
        user_id="s6-test-user",
        q=prompt,
        params=params,
    )


# ===========================================================================
# Startup version gate (§3.3)
# ===========================================================================


@pytest.mark.unit
class TestVersionFloorBoundary:
    def test_floor_is_inclusive(self) -> None:
        """``min_version`` is a floor the installed version is allowed to sit ON.

        Both existing cases are far from the boundary (a 99.0.0 floor, or the
        pinned 0.4.0 against a newer install), so flipping ``<`` to ``<=`` —
        which rejects an exactly-matching version — changed nothing. A pin bumped
        to exactly the deployed version would then refuse to start.
        """
        installed = router_mod._installed_aphelion_version()
        assert assert_aphelion_version(min_version=installed) == installed

    def test_one_patch_below_installed_still_passes(self) -> None:
        assert assert_aphelion_version(min_version="0.0.1") is not None

    def test_version_components_compare_numerically_not_per_digit(self) -> None:
        """``0.4.10`` is ABOVE ``0.4.9`` — components are numbers, not digits.

        Splitting per digit gives ``(0, 4, 1, 0)`` vs ``(0, 4, 9)``, which
        compares the wrong way and would make the router refuse to start against
        the first double-digit patch release of Aphelion.
        """
        assert _parse_version("0.4.10") == (0, 4, 10)
        assert _parse_version("0.4.10") > _parse_version("0.4.9")
        assert _parse_version("1.10.0") > _parse_version("1.9.0")


# ===========================================================================
# Package-dir validation (§4.4)
# ===========================================================================


@pytest.mark.unit
class TestPackageDirUnreadable:
    def test_unreadable_dir_is_perm_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An existing, non-traversing directory we cannot READ must still fail.

        Windows ACLs make ``os.access(..., R_OK)`` effectively always true for
        the owning user, so the branch is unreachable end-to-end on this
        platform and no test covered it — deleting the check outright left the
        suite green. Patching ``os.access`` exercises the branch directly: the
        point being pinned is that an unreadable dir is rejected with the
        spec-named ``perm_denied`` reason rather than silently accepted and
        degraded to an empty corpus.
        """
        d = tmp_path / "unreadable"
        d.mkdir()
        real_access = router_mod.os.access

        def _deny(path: Any, mode: int, **kwargs: Any) -> bool:
            if Path(path) == d and mode == router_mod.os.R_OK:
                return False
            return real_access(path, mode, **kwargs)

        monkeypatch.setattr(router_mod.os, "access", _deny)

        before = _counter_value(router_mod.PACKAGE_DIR_ERRORS, reason="perm_denied")
        with pytest.raises(AphelionUnreachableError) as exc:
            validate_package_dir(d)
        assert exc.value.reason == "package_dir_inaccessible"
        assert _counter_value(router_mod.PACKAGE_DIR_ERRORS, reason="perm_denied") == before + 1

    def test_readable_dir_is_accepted(self, tmp_path: Path) -> None:
        """Positive twin: without the patch the same directory validates."""
        d = tmp_path / "readable"
        d.mkdir()
        assert validate_package_dir(d) == d.resolve()

    def test_symlinked_dir_is_returned_resolved(self, tmp_path: Path) -> None:
        """The accepted path is the RESOLVED target, not the link that named it.

        ``tmp_path`` is already canonical, so the existing happy-path assertion
        (``resolved == d.resolve()``) holds just as well without the
        ``.resolve()`` call. A symlink is the case that separates them: the
        router stores this value and globs the corpus through it, so returning
        the unresolved alias means two aliases of one corpus are treated as two
        different package dirs (and get two different subject-index files).
        """
        real = tmp_path / "real_packages"
        real.mkdir()
        link = tmp_path / "link_packages"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unprivileged on this platform")

        result = validate_package_dir(link)
        assert result == real.resolve()
        assert result != link


# ===========================================================================
# Read-mode dispatch (#71 Part B)
# ===========================================================================


@pytest.mark.unit
class TestReadModeDispatch:
    def test_blank_explicit_subject_is_not_an_explicit_subject(self) -> None:
        """``params['subject'] = ''`` is absence, not "the empty subject".

        ``_explicit_subject`` is the whole read-mode switch. Dropping its
        truthiness check routes a blank subject down the exact-subject path,
        where it can only ever match nothing — a free-text prompt would be
        answered with a guaranteed empty result instead of being resolved.
        """
        assert _explicit_subject(_freetext_query("retrieval quality", {"subject": ""})) is None
        assert _explicit_subject(_freetext_query("retrieval quality", {})) is None
        assert _explicit_subject(_freetext_query("retrieval quality", None)) is None
        assert _explicit_subject(_freetext_query("retrieval quality", {"subject": 7})) is None

    def test_non_blank_explicit_subject_is_honoured(self) -> None:
        assert _explicit_subject(_exact_query("retrieval-quality")) == "retrieval-quality"

    def test_blank_subject_takes_the_freetext_path(
        self, package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end twin of the above: a blank subject reaches the resolver."""
        router = _make_router(package_dir, audit_conn)
        monkeypatch.setattr(
            router_mod.subject_index,
            "load_or_rebuild",
            lambda package_dir, scan_fn: _index_with(("alpha-topic",)),
        )
        monkeypatch.setattr(router._adapter, "query", lambda request: _evidence_with_id("c1"))

        evidence = router.query(_freetext_query("alpha topic", {"subject": ""}))

        assert any(note == "resolver=token_overlap" for note in evidence.notes)
        assert len(evidence.hits) == 1


# ===========================================================================
# Free-text merge (#71 Part B, design doc D4)
# ===========================================================================


def _index_with(subjects: tuple[str, ...], package_file: str = "shared.aphelion.tar"):
    """A real :class:`SubjectIndex` mapping every subject to one shared package.

    One package carrying claims for several subjects is the ordinary shape (the
    M6 packer groups by producer run, not by subject), and it is the shape that
    makes the same claim reachable from two candidate subjects.
    """
    entries = tuple(
        subject_index.IndexEntry(
            subject=subject,
            package_id="01963f7d-7000-7000-8000-aaaa00000001",
            claim_id=f"claim-{subject}",
            package_file=package_file,
        )
        for subject in subjects
    )
    return subject_index.SubjectIndex(
        entries=entries,
        packages=(subject_index.PackageStat(name=package_file, size=1, mtime_ns=1),),
        built_at=0.0,
    )


def _evidence_with_id(claim_id: str) -> RetrievalEvidence:
    """Adapter evidence carrying one hit, with a caller-chosen claim id."""
    return RetrievalEvidence(
        hits=({"id": claim_id, "text": "body", "subject": "alpha-topic"},),
        stages=("aphelion_v03_r4",),
    )


@pytest.mark.unit
class TestFreeTextMergeDedup:
    def test_same_claim_under_two_subjects_is_returned_once(
        self, package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D4 dedup: hits are merged across candidates and deduped by claim id.

        Every existing free-text case resolves candidates whose R4 reads return
        disjoint claims, so the dedup branch was never executed (coverage
        confirmed it) and removing it changed no result. Here both candidate
        subjects live in one package and R4 surfaces the same claim for each,
        which is what the dedup exists for.
        """
        router = _make_router(package_dir, audit_conn)
        monkeypatch.setattr(
            router_mod.subject_index,
            "load_or_rebuild",
            lambda package_dir, scan_fn: _index_with(("alpha-topic", "beta-topic")),
        )
        calls: list[str] = []

        def _adapter_query(request: QueryRequest) -> RetrievalEvidence:
            calls.append(request.params["subject"])
            return _evidence_with_id("shared-claim-1")

        monkeypatch.setattr(router._adapter, "query", _adapter_query)

        evidence = router.query(_freetext_query("alpha topic beta topic"))

        # Both candidates were actually read (otherwise the dedup is untested).
        assert sorted(calls) == ["alpha-topic", "beta-topic"]
        assert [hit["id"] for hit in evidence.hits] == ["shared-claim-1"]

    def test_distinct_claims_across_subjects_are_both_kept(
        self, package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive twin: dedup must not swallow genuinely different claims."""
        router = _make_router(package_dir, audit_conn)
        monkeypatch.setattr(
            router_mod.subject_index,
            "load_or_rebuild",
            lambda package_dir, scan_fn: _index_with(("alpha-topic", "beta-topic")),
        )
        monkeypatch.setattr(
            router._adapter,
            "query",
            lambda request: _evidence_with_id(f"claim-{request.params['subject']}"),
        )

        evidence = router.query(_freetext_query("alpha topic beta topic"))

        assert sorted(hit["id"] for hit in evidence.hits) == [
            "claim-alpha-topic",
            "claim-beta-topic",
        ]


# ===========================================================================
# Observability envelope (§4.5)
# ===========================================================================


@pytest.mark.unit
class TestReadOutcomeCounter:
    def test_typed_failure_counts_as_an_error_not_a_success(
        self, package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``parallax_apex_read{result}`` splits failed reads from served ones.

        The failure-mode tests all assert the per-reason error counter and stop
        there, so labelling a failed read ``result="success"`` left them green —
        and would make the error RATE (errors over total reads, the shape the
        §4.2 alert uses) read as a clean 100% success.
        """
        router = _make_router(package_dir, audit_conn)

        def _boom(request: QueryRequest) -> RetrievalEvidence:
            raise AphelionUnreachableError("package_corrupt")

        monkeypatch.setattr(router._adapter, "query", _boom)

        before_err = _counter_value(router_mod.READ_TOTAL, result="error")
        before_ok = _counter_value(router_mod.READ_TOTAL, result="success")
        with pytest.raises(AphelionUnreachableError):
            router.query(_exact_query("retrieval-quality"))

        assert _counter_value(router_mod.READ_TOTAL, result="error") == before_err + 1
        assert _counter_value(router_mod.READ_TOTAL, result="success") == before_ok

    def test_successful_read_counts_as_a_success(
        self, package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive twin: the same counter must move the other way on a good read."""
        router = _make_router(package_dir, audit_conn)
        monkeypatch.setattr(router._adapter, "query", lambda request: _evidence_with_id("c1"))

        before_err = _counter_value(router_mod.READ_TOTAL, result="error")
        before_ok = _counter_value(router_mod.READ_TOTAL, result="success")
        router.query(_exact_query("retrieval-quality"))

        assert _counter_value(router_mod.READ_TOTAL, result="success") == before_ok + 1
        assert _counter_value(router_mod.READ_TOTAL, result="error") == before_err


@pytest.mark.unit
class TestLatencyIsMilliseconds:
    def test_observed_latency_is_in_milliseconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The histogram is declared in ms and its buckets straddle the 100ms SLA.

        Observing seconds instead would put every real read in the bottom
        bucket, so ``histogram_quantile`` reports a p99 of ~1ms forever and the
        §4.2 breach alert can never fire. Nothing asserted the magnitude, only
        that a sample existed.
        """
        ticks = iter([10.0, 10.25])  # 250ms of wall clock
        monkeypatch.setattr(router_mod.time, "perf_counter", lambda: next(ticks))

        child = router_mod.READ_LATENCY.labels(result="success")
        before = child._sum.get()  # type: ignore[attr-defined]
        start = router_mod.time.perf_counter()
        ApexPublicReadRouter._record(start, "success")

        assert child._sum.get() - before == pytest.approx(250.0)  # type: ignore[attr-defined]


@pytest.mark.unit
class TestAuditWriteFailureCause:
    def test_causeless_audit_failure_is_labelled_unknown(self) -> None:
        """``cause`` must never be the empty string on this counter.

        The audit-abort test raises through a real ``__cause__``, so the
        ``or "unknown"`` fallback was never taken. An empty ``cause`` is
        indistinguishable from the primed zero series
        (``audit_write_failures{cause=""}``), which is exactly the label a
        reader treats as "nothing has happened here".
        """
        err = AphelionUnreachableError("audit_db_write_failed")
        assert err.__cause__ is None

        before = _counter_value(router_mod.AUDIT_WRITE_FAILURES, cause="unknown")
        before_blank = _counter_value(router_mod.AUDIT_WRITE_FAILURES, cause="")
        ApexPublicReadRouter._record_error(err)

        assert _counter_value(router_mod.AUDIT_WRITE_FAILURES, cause="unknown") == before + 1
        assert _counter_value(router_mod.AUDIT_WRITE_FAILURES, cause="") == before_blank


@pytest.mark.integration
class TestEmptyResultCauseOnExactPath:
    def test_empty_corpus_on_the_exact_path_is_caused_empty_corpus(
        self, package_dir: Path, audit_conn: sqlite3.Connection
    ) -> None:
        """An empty corpus must be labelled ``empty_corpus``, not ``no_matching_claim``.

        ``ApexStuckEmptyCorpus`` selects ``cause="empty_corpus"`` by equality —
        it is the one label value the module primes at zero for exactly this
        reason. The existing empty-corpus test asserts only the raw
        ``empty_corpus`` counter, so the flag that picks this label could be
        stuck false and the alert would go quiet while the corpus stayed empty.
        """
        router = _make_router(package_dir, audit_conn)

        before_empty = _counter_value(router_mod.EMPTY_RESULT, cause="empty_corpus")
        before_nomatch = _counter_value(router_mod.EMPTY_RESULT, cause="no_matching_claim")
        evidence = router.query(_exact_query("retrieval-quality"))

        assert evidence.hits == ()
        assert _counter_value(router_mod.EMPTY_RESULT, cause="empty_corpus") == before_empty + 1
        assert (
            _counter_value(router_mod.EMPTY_RESULT, cause="no_matching_claim") == before_nomatch
        )
