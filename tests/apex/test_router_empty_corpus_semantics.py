"""S5 riders from the 2026-08-16 S6 report — Apex router negative-result semantics.

Two findings, both cases of a signal that says one thing and does another.

**S6-F1 — the empty-corpus counter's HELP text.**
``parallax_apex_empty_corpus`` documented itself as "First-read-per-process
detection of an accessible-but-empty corpus", but no first-read guard exists
anywhere in the module: both ``EMPTY_CORPUS.inc()`` sites — the exact-subject
loader in ``_load_candidate_claims`` and the index check in ``_query_freetext``
— fire on *every* read against an empty corpus. An operator reading the HELP
off ``/metrics`` would take the series for a count of processes when it is a
count of reads, which scales with traffic instead of with deploys. The
``ApexStuckEmptyCorpus`` rule comment leaned on the same wrong claim to justify
selecting ``empty_result{cause="empty_corpus"}`` instead. The HELP and the rule
comment now describe per-read semantics; the tests below pin the behaviour that
makes them true.

**S6-F2 — the free-text negative-result marker.**
``_empty_evidence`` exists so a free-text miss is shaped like an exact-subject
miss: it stamps ``conflict_class=NOT_FOUND`` as the first note. Two of the three
negative paths went through it — empty corpus, and zero resolved candidates.
The third, where candidates resolve and their packages are found but the
per-candidate R4 reads yield nothing, fell through to the generic tail return
and produced a zero-hit ``RetrievalEvidence`` carrying no NOT_FOUND note at all.
A consumer keying on that marker therefore recognised two of the three miss
kinds and silently mis-classified the third as something other than a miss.

The three paths are asserted against each other rather than one at a time, so
re-introducing a second shape for "no hits" fails here rather than at whichever
consumer notices first.

Fixture style follows ``test_router_mutation_harden.py``: a hand-built
``SubjectIndex`` plus a stubbed adapter, rather than real signed packages. The
branch under test is "``merged`` is empty after the per-candidate loop", and
driving that through real R4 validity rules would couple the test to claim
lifecycle semantics that are not what is being pinned.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import prometheus_client
import pytest

from parallax.apex import router as router_mod
from parallax.apex import subject_index
from parallax.apex.audit_db import open_audit_db
from parallax.apex.router import ApexPublicReadRouter
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest
from parallax.router.types import QueryType

#: The exact note ``_empty_evidence`` stamps. A literal rather than a call to
#: ``_empty_evidence()``, because deriving the expectation from the function
#: under test would let a renamed marker satisfy every assertion below at once.
NOT_FOUND_NOTE = "conflict_class=NOT_FOUND"


def _counter_value(metric: Any, **labels: str) -> float:
    child = metric.labels(**labels) if labels else metric
    return child._value.get()  # type: ignore[attr-defined]


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    """An accessible, empty package directory — the fresh-deploy state."""
    d = tmp_path / "packages"
    d.mkdir()
    return d


@pytest.fixture
def audit_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = open_audit_db(tmp_path / "s5_audit.db", validate=False)
    yield conn
    conn.close()


def _make_router(package_dir: Path, audit_conn: sqlite3.Connection) -> ApexPublicReadRouter:
    return ApexPublicReadRouter(package_dir=package_dir, audit_conn_provider=lambda: audit_conn)


def _exact_query(subject: str = "retrieval-quality") -> QueryRequest:
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT,
        user_id="s5-test-user",
        q=subject,
        params={"subject": subject},
    )


def _freetext_query(prompt: str) -> QueryRequest:
    """``q`` set, no ``params['subject']`` — the resolver path."""
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT,
        user_id="s5-test-user",
        q=prompt,
        params=None,
    )


def _index_with(
    subjects: tuple[str, ...], package_file: str = "shared.aphelion.tar"
) -> subject_index.SubjectIndex:
    """A non-empty index mapping every subject to one shared package."""
    entries = tuple(
        subject_index.IndexEntry(
            subject=subject,
            package_id="01963f7d-7000-7000-8000-5f5f00000001",
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


def _no_hits(_request: QueryRequest) -> RetrievalEvidence:
    """An adapter whose R4 read resolves cleanly and finds nothing."""
    return RetrievalEvidence(hits=(), stages=("aphelion_v03_r4",), notes=())


def _empty_corpus_documentation() -> str:
    """HELP text for ``parallax_apex_empty_corpus`` as the wire renders it."""
    for metric in prometheus_client.REGISTRY.collect():
        if metric.name == "parallax_apex_empty_corpus":
            return metric.documentation
    raise AssertionError("parallax_apex_empty_corpus is not registered")


# ---------------------------------------------------------------------------
# S6-F1 — the HELP text must match the two inc() sites
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_corpus_help_text_describes_per_read_semantics() -> None:
    """The HELP text may not promise a first-read-per-process guard.

    There is no such guard: both call sites increment unconditionally when the
    corpus is empty. An operator who believes the old wording reads the counter
    as "processes that booted onto an empty corpus" when it is in fact "reads
    that hit one" — off by a factor of the request rate, and in the wrong
    direction for a stuck-empty deploy, which is the situation the series
    exists to make visible.
    """
    documentation = _empty_corpus_documentation()

    assert "first-read" not in documentation.lower()
    assert "per-process" not in documentation.lower()
    assert "every" in documentation.lower(), (
        "the HELP must state the per-read semantics the code actually has"
    )


@pytest.mark.integration
def test_two_consecutive_empty_corpus_reads_increment_the_counter_by_two(
    package_dir: Path, audit_conn: sqlite3.Connection
) -> None:
    """Two exact-subject reads on an empty corpus advance the counter by 2.

    This is the behaviour the corrected HELP describes. The pre-existing
    ``test_empty_corpus_returns_empty_and_counts`` reads once and asserts +1,
    which a genuine first-read-per-process guard would satisfy too — so it
    could never have caught the drift in either direction.
    """
    router = _make_router(package_dir, audit_conn)
    before = _counter_value(router_mod.EMPTY_CORPUS)

    first = router.query(_exact_query())
    second = router.query(_exact_query())

    assert first.hits == ()
    assert second.hits == ()
    assert _counter_value(router_mod.EMPTY_CORPUS) == before + 2


@pytest.mark.integration
def test_two_consecutive_empty_corpus_freetext_reads_increment_by_two(
    package_dir: Path, audit_conn: sqlite3.Connection
) -> None:
    """The free-text inc() site has the same per-read semantics.

    Both sites are covered by the one HELP string, so both are pinned. This one
    reaches the counter through the subject index rather than the package glob.
    """
    router = _make_router(package_dir, audit_conn)
    before = _counter_value(router_mod.EMPTY_CORPUS)

    router.query(_freetext_query("anything at all"))
    router.query(_freetext_query("anything at all"))

    assert _counter_value(router_mod.EMPTY_CORPUS) == before + 2


# ---------------------------------------------------------------------------
# S6-F2 — every free-text miss carries the same NOT_FOUND marker
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_freetext_miss_with_zero_candidates_carries_not_found(
    package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline. This path already routed through ``_empty_evidence``.

    Green before and after the fix — it is here as the reference shape the
    other two are compared against.
    """
    router = _make_router(package_dir, audit_conn)
    monkeypatch.setattr(
        router_mod.subject_index,
        "load_or_rebuild",
        lambda package_dir, scan_fn: _index_with(("alpha-topic",)),
    )

    evidence = router.query(_freetext_query("zzzz qqqq xxxx"))

    assert evidence.hits == ()
    assert evidence.notes[0] == NOT_FOUND_NOTE


@pytest.mark.integration
def test_freetext_miss_with_resolved_candidates_carries_the_same_not_found(
    package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third miss path: candidates resolve, their R4 reads return nothing.

    RED before the fix. This path fell through to the generic tail return,
    which builds its notes from the resolver alone and never stamped
    ``conflict_class=NOT_FOUND`` — so a zero-hit result came back not looking
    like a miss, on the one negative path that reaches real packages.
    """
    router = _make_router(package_dir, audit_conn)
    monkeypatch.setattr(
        router_mod.subject_index,
        "load_or_rebuild",
        lambda package_dir, scan_fn: _index_with(("alpha-topic",)),
    )
    monkeypatch.setattr(router._adapter, "query", _no_hits)  # noqa: SLF001

    evidence = router.query(_freetext_query("alpha topic"))

    assert evidence.hits == ()
    assert evidence.notes[0] == NOT_FOUND_NOTE, (
        "the marker must sit where _empty_evidence puts it, so both miss kinds "
        "are identical to a consumer reading notes[0]"
    )
    # Routing through _empty_evidence must not cost the resolver context.
    assert any(note == "resolver=token_overlap" for note in evidence.notes)
    assert any(note.startswith("candidates=") for note in evidence.notes)
    assert any(note.startswith("subjects=") for note in evidence.notes)


@pytest.mark.integration
def test_a_resolved_candidate_with_hits_is_not_marked_not_found(
    package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the fix: a real hit must NOT carry the miss marker.

    Routing the zero-hit case through ``_empty_evidence`` is only correct if it
    is conditional on there being no hits — stamping NOT_FOUND unconditionally
    would be the mirror-image defect, and a consumer that treats the marker as
    authoritative would then discard good results.
    """
    router = _make_router(package_dir, audit_conn)
    monkeypatch.setattr(
        router_mod.subject_index,
        "load_or_rebuild",
        lambda package_dir, scan_fn: _index_with(("alpha-topic",)),
    )
    monkeypatch.setattr(
        router._adapter,  # noqa: SLF001
        "query",
        lambda request: RetrievalEvidence(
            hits=({"id": "c1", "subject": "alpha-topic", "text": "a claim"},),
            stages=("aphelion_v03_r4",),
            notes=(),
        ),
    )

    evidence = router.query(_freetext_query("alpha topic"))

    assert len(evidence.hits) == 1
    assert NOT_FOUND_NOTE not in evidence.notes


@pytest.mark.integration
def test_all_three_freetext_miss_kinds_agree_on_the_marker(
    package_dir: Path, audit_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty corpus, no candidates, and no claims must be indistinguishable
    on the NOT_FOUND marker and on the stage list.

    Asserted together rather than as three separate expectations so a
    divergence surfaces as one failure that names the odd path out, and so a
    future fourth negative path has an obvious place to be added.
    """
    router = _make_router(package_dir, audit_conn)
    empty_corpus = router.query(_freetext_query("anything"))

    monkeypatch.setattr(
        router_mod.subject_index,
        "load_or_rebuild",
        lambda package_dir, scan_fn: _index_with(("alpha-topic",)),
    )
    no_candidates = router.query(_freetext_query("zzzz qqqq xxxx"))

    monkeypatch.setattr(router._adapter, "query", _no_hits)  # noqa: SLF001
    no_claims = router.query(_freetext_query("alpha topic"))

    for label, evidence in (
        ("empty_corpus", empty_corpus),
        ("no_candidates", no_candidates),
        ("no_claims", no_claims),
    ):
        assert evidence.hits == (), f"{label} must return zero hits"
        assert evidence.notes[0] == NOT_FOUND_NOTE, f"{label} lost the NOT_FOUND marker"
        assert evidence.stages == ("aphelion_v03_r4",), f"{label} reported a different stage"
