"""Mutation-hardening for ``parallax.router.aphelion_adapter`` (overnight-20260816 S8).

Additive companion to ``test_aphelion_adapter.py``. Every test below was written
against a semantic mutant that the pre-existing suite let through.

The shape of the gap: the existing file drives the adapter over *well-formed*
claims that carry every optional field, then asserts the headline outputs — the
conflict class, the primary claim id, the ``audit_db_ref`` sha256. That leaves
three families unpinned.

  * **Subject resolution.** Every existing adapter case supplies ``q`` and no
    ``params``, so the precedence rules in ``_resolve_subject`` (a blank
    explicit subject must not win; an empty ``q`` must fall back to the user id)
    are never observed.

  * **Hit construction.** ``_build_hit``'s defaults and shaping are asserted
    only where the claim already supplies the value: the ``polarity`` default,
    the package id inside the provenance sentence, the ``created_at`` key's
    *absence*, the fact that ``full`` is a snapshot rather than an alias onto
    the caller's mapping, and the whitespace handling in ``_claim_content``
    (a padded body, a whitespace-only body, an empty ``title``) all go
    unobserved.

  * **The fail-closed audit-write fence.** ``query`` maps four distinct failure
    modes onto four distinct ``AphelionUnreachableError.reason`` tags precisely
    so DualReadRouter classifies them as ``aphelion_unreachable`` (and trips the
    circuit breaker) instead of the silent ``primary_only``. No existing case
    reaches any of them: the audit-conn provider never raises, no envelope id
    ever collides, and no connection ever arrives mid-transaction.

Also pinned here: the loader contract (every candidate is validated, not just
the first; a one-shot iterable is materialized before use) and the single-
timestamp rule that keeps ``audit_row.ts`` equal to ``envelope.created_at``.
"""

from __future__ import annotations

import pathlib
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from aphelion.read_adapter import ConflictClass

from parallax.apex.audit_db import open_audit_db
from parallax.router import aphelion_adapter as adapter_mod
from parallax.router.aphelion_adapter import (
    AphelionReadAdapter,
    AphelionUnreachableError,
    _conflict_class_to_outcome,
)
from parallax.router.contracts import QueryRequest
from parallax.router.types import QueryType

_PACKAGE_ID = "01963f7d-7000-7000-8000-0000000000aa"
_OLDER_ID = "01963f7d-7000-7000-8000-0000000000b0"
_NEWER_ID = "01963f7d-7000-7000-8000-0000000000b1"


def _claim(
    *,
    claim_id: str = "01963f7d-7000-7000-8000-0000000000c1",
    subject: str = "subject:foo",
    polarity: str | None = "affirm",
    package_id: str | None = _PACKAGE_ID,
    **extra: Any,
) -> dict[str, Any]:
    """Minimal v0.3-valid claim frontmatter; ``None`` omits the field entirely."""
    out: dict[str, Any] = {"claim_id": claim_id, "subject": subject}
    if polarity is not None:
        out["polarity"] = polarity
    if package_id is not None:
        out["package_id"] = package_id
    out.update(extra)
    return out


def _request(
    *,
    user_id: str = "u1",
    q: str = "subject:foo",
    params: Mapping[str, Any] | None = None,
) -> QueryRequest:
    return QueryRequest(
        query_type=QueryType.RECENT_CONTEXT, user_id=user_id, q=q, params=params
    )


def _supersession_pair() -> list[dict[str, Any]]:
    """Two claims where the newer supersedes the older — the emit-an-envelope path."""
    return [
        _claim(claim_id=_OLDER_ID),
        _claim(claim_id=_NEWER_ID, supersedes=[_OLDER_ID]),
    ]


@pytest.fixture()
def audit_conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    conn = open_audit_db(tmp_path / "audit.db", validate=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def audit_conn_provider(
    audit_conn: sqlite3.Connection,
) -> Callable[[], sqlite3.Connection]:
    return lambda: audit_conn


def _adapter(
    provider: Callable[[], sqlite3.Connection],
    claims: Iterable[Mapping[str, Any]] | Callable[[QueryRequest], Any],
) -> AphelionReadAdapter:
    loader = claims if callable(claims) else (lambda _r: list(claims))
    return AphelionReadAdapter(audit_conn_provider=provider, claim_loader=loader)


# ---------------------------------------------------------------------------
# _resolve_subject — precedence rules no existing case supplies params for
# ---------------------------------------------------------------------------


def test_blank_explicit_subject_falls_through_to_the_query_string(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``params={"subject": ""}`` is not a subject — ``q`` must still be used.

    Accepting the empty string would resolve every such read against the empty
    subject, which matches no claim: a silent, corpus-wide NOT_FOUND.
    """
    adapter = _adapter(audit_conn_provider, [_claim(subject="subject:from-q")])
    adapter.query(_request(q="subject:from-q", params={"subject": ""}))

    env = adapter.last_envelope
    assert env is not None, "resolving to the blank subject would emit nothing"
    assert env.payload["subject"] == "subject:from-q"


def test_empty_query_falls_back_to_the_user_id_as_the_scope_tag(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``q`` defaults to ``""`` on QueryRequest — the user id is the last resort."""
    adapter = _adapter(audit_conn_provider, [_claim(subject="u-scope-tag")])
    adapter.query(_request(user_id="u-scope-tag", q=""))

    env = adapter.last_envelope
    assert env is not None, "an empty subject would resolve nothing"
    assert env.payload["subject"] == "u-scope-tag"


# ---------------------------------------------------------------------------
# _claim_content / _string_field — whitespace and empty-string handling
# ---------------------------------------------------------------------------


def test_body_content_is_whitespace_trimmed(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """A body carries the surrounding markdown newlines; the hit text must not."""
    claim = _claim(body="\n  Padded body statement.  \n")
    adapter = _adapter(audit_conn_provider, [claim])
    hit = adapter.query(_request()).hits[0]

    assert hit["text"] == "Padded body statement."
    assert hit["content_source"] == "body"


def test_whitespace_only_body_falls_through_to_the_title(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """A body of pure whitespace is not content — it must degrade to the title.

    Treating it as content would surface an empty-text hit whose
    ``content_source`` claims to be the body, hiding the degradation the
    ``content_source`` field exists to make visible.
    """
    claim = _claim(body="   \n \t ", title="The claim title")
    adapter = _adapter(audit_conn_provider, [claim])
    hit = adapter.query(_request()).hits[0]

    assert hit["text"] == "The claim title"
    assert hit["content_source"] == "title"


def test_blank_title_is_not_a_content_source(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``_string_field`` treats an empty string as absent, so a blank title
    degrades to the subject label rather than to empty text."""
    claim = _claim(subject="subject:blank-title", title="")
    adapter = _adapter(audit_conn_provider, [claim])
    hit = adapter.query(_request(q="subject:blank-title")).hits[0]

    assert hit["text"] == "subject:blank-title"
    assert hit["content_source"] == "subject"


# ---------------------------------------------------------------------------
# _build_hit — defaults, provenance, snapshot semantics, optional keys
# ---------------------------------------------------------------------------


def test_polarity_defaults_to_affirm_when_the_claim_omits_it(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``polarity`` is optional in v0.3; an absent one means affirm, not negate.

    Defaulting the other way inverts the meaning of every polarity-less claim
    a consumer reads off the hit.
    """
    adapter = _adapter(audit_conn_provider, [_claim(polarity=None)])
    hit = adapter.query(_request()).hits[0]

    assert hit["polarity"] == "affirm"


def test_hit_provenance_names_the_claim_subject_polarity_and_package(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``evidence`` is the L2 provenance sentence — it must identify the source.

    The package id is the only field that says *which* Aphelion package the
    claim came from; dropping it leaves an operator unable to trace a surfaced
    claim back to an ingested artifact.
    """
    claim = _claim(
        claim_id="01963f7d-7000-7000-8000-0000000000d1",
        subject="subject:provenance",
        polarity="negate",
    )
    adapter = _adapter(audit_conn_provider, [claim])
    hit = adapter.query(_request(q="subject:provenance")).hits[0]

    evidence = hit["evidence"]
    assert isinstance(evidence, str)
    assert "01963f7d-7000-7000-8000-0000000000d1" in evidence
    assert "subject:provenance" in evidence
    assert "polarity=negate" in evidence
    assert f"package={_PACKAGE_ID}" in evidence


def test_hit_full_is_a_snapshot_not_an_alias_of_the_loader_mapping(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``full`` is a shallow *copy* — a consumer editing it must not reach back
    into the loader's claim mapping (which the loader may cache and reuse)."""
    claim = _claim(subject="subject:snapshot")
    adapter = _adapter(audit_conn_provider, [claim])
    hit = adapter.query(_request(q="subject:snapshot")).hits[0]

    assert hit["full"] is not claim
    hit["full"]["subject"] = "tampered"
    assert claim["subject"] == "subject:snapshot"


def test_created_at_key_is_omitted_when_the_claim_has_none(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``created_at`` is optional: absent means the key is missing, not ``None``.

    Emitting ``created_at: None`` turns "this claim is undated" into a value a
    consumer must special-case, and JSON-serializes as an explicit null.
    """
    without = _adapter(audit_conn_provider, [_claim()])
    hit = without.query(_request()).hits[0]
    assert "created_at" not in hit

    dated = _adapter(
        audit_conn_provider,
        [
            _claim(
                claim_id="01963f7d-7000-7000-8000-0000000000d2",
                created_at="2026-01-02T03:04:05Z",
            )
        ],
    )
    dated_hit = dated.query(_request()).hits[0]
    assert dated_hit["created_at"] == "2026-01-02T03:04:05Z"


# ---------------------------------------------------------------------------
# query — the claim_loader contract
# ---------------------------------------------------------------------------


def test_every_candidate_claim_is_schema_validated_not_just_the_first(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """The validator gate covers the whole candidate set.

    Validating only the head would let a malformed claim past the gate and into
    R4 detection whenever a well-formed claim happened to sort first.
    """
    good = _claim(claim_id=_OLDER_ID)
    bad = {
        "claim_id": "01963f7d-7000-7000-8000-0000000000e0",
        "subject": "subject:foo",
        "polarity": "affirm",
        "conflict_class": "ambiguity",  # reserved derivation field (spec §7)
    }
    adapter = _adapter(audit_conn_provider, [good, bad])

    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "claim_schema_error"


def test_loader_may_return_a_one_shot_iterable(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``claim_loader`` is typed to return an ``Iterable``, so a generator is
    legal — the adapter must materialize it before the validation pass consumes
    it, or R4 detection runs against an exhausted iterator and every read
    silently degrades to NOT_FOUND."""

    def generator_loader(_req: QueryRequest) -> Iterable[Mapping[str, Any]]:
        yield from _supersession_pair()

    adapter = _adapter(audit_conn_provider, generator_loader)
    evidence = adapter.query(_request())

    assert evidence.notes == ("conflict_class=supersession",)
    assert [hit["id"] for hit in evidence.hits] == [_NEWER_ID]
    assert adapter.last_envelope is not None


def test_primary_without_a_package_id_surfaces_hits_but_emits_no_envelope(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """Both ids are required to emit: the audit row cannot be anchored without
    a package id, so a package-less primary returns evidence and stops.

    Requiring only *one* of them would push a row with a missing required field
    into ``canonicalize_row`` and turn a benign scope-cut into an
    ``audit_row_invalid`` outage.
    """
    adapter = _adapter(audit_conn_provider, [_claim(package_id=None)])
    evidence = adapter.query(_request())

    assert [hit["id"] for hit in evidence.hits] == ["01963f7d-7000-7000-8000-0000000000c1"]
    assert adapter.last_envelope is None
    assert adapter.last_audit_row is None


# ---------------------------------------------------------------------------
# query — audit row field derivation
# ---------------------------------------------------------------------------


def test_audit_row_session_id_anchors_on_the_request_user_id(
    audit_conn_provider: Callable[[], sqlite3.Connection],
) -> None:
    """``session_id`` is the per-caller correlation key, not the subject label."""
    adapter = _adapter(audit_conn_provider, _supersession_pair())
    adapter.query(_request(user_id="user-42", q="subject:foo"))

    row = adapter.last_audit_row
    assert row is not None
    assert row.data["session_id"] == "user-42"


def test_audit_row_ts_and_envelope_created_at_come_from_one_timestamp(
    audit_conn_provider: Callable[[], sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single ``emit_ts`` feeds both fields (audit-db-path-config.md §6.1).

    Existing coverage compares the two values on a real clock, where they agree
    by luck whenever both computations land in the same second. Driving a
    strictly increasing clock makes the invariant observable: recomputing the
    envelope timestamp separates the two.
    """
    stamps = iter(
        [
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            "2026-01-01T00:00:02Z",
        ]
    )
    monkeypatch.setattr(adapter_mod, "_utc_now_iso_z", lambda: next(stamps))

    adapter = _adapter(audit_conn_provider, _supersession_pair())
    adapter.query(_request())

    env = adapter.last_envelope
    row = adapter.last_audit_row
    assert env is not None and row is not None
    assert row.data["ts"] == env.created_at == "2026-01-01T00:00:00Z"


def test_conflict_class_maps_only_not_found_to_a_miss() -> None:
    """``outcome`` is the audit row's hit/miss enum (audit-db-path-config.md §6.1).

    Only ``NOT_FOUND`` is a miss; every other class means claims were found.
    The mapping is asserted directly because the NOT_FOUND branch is
    unreachable through ``query`` (a missing primary returns before the audit
    row is built), so a whole-read test can never observe it.
    """
    assert _conflict_class_to_outcome(ConflictClass.NOT_FOUND) == "miss"
    for cc in ConflictClass:
        if cc is not ConflictClass.NOT_FOUND:
            assert _conflict_class_to_outcome(cc) == "hit", cc


# ---------------------------------------------------------------------------
# query — the fail-closed audit-write fence and its reason tags
# ---------------------------------------------------------------------------


def test_audit_conn_provider_failure_is_wrapped_not_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider is invoked inside the fence on purpose.

    It runs on the DualReadRouter worker thread and can fail there (e.g.
    ``open_audit_db`` raising ``AuditDbConfigError``). An unwrapped escape
    reaches DualReadRouter as an *unexpected* exception, which it classifies as
    ``primary_only`` — losing both the ``aphelion_unreachable`` signal and the
    circuit-breaker increment.
    """

    def exploding_provider() -> sqlite3.Connection:
        raise RuntimeError("audit db unavailable on this worker thread")

    adapter = AphelionReadAdapter(
        audit_conn_provider=exploding_provider,
        claim_loader=lambda _r: _supersession_pair(),
    )

    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "audit_db_write_failed"
    assert adapter.last_envelope is None
    assert adapter.last_audit_row is None


def test_duplicate_envelope_message_id_reports_the_integrity_reason(
    audit_conn_provider: Callable[[], sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UNIQUE collision on ``envelope_message_id`` is its own reason tag.

    ``audit_row`` declares ``envelope_message_id TEXT NOT NULL UNIQUE``, so a
    repeated id is a distinct operational fault from a disk-full write failure
    and must not be folded into ``audit_db_write_failed``. Pinning the message
    id makes the collision reachable (the audit-row schema requires a UUID v4
    here, unlike the v7 claim/package ids).
    """
    fixed = uuid.UUID("3f2a1b0c-4d5e-4f60-8a91-0123456789ab")
    monkeypatch.setattr(adapter_mod, "uuid", SimpleNamespace(uuid4=lambda: fixed))

    adapter = _adapter(audit_conn_provider, _supersession_pair())
    adapter.query(_request())
    assert adapter.last_envelope is not None

    with pytest.raises(AphelionUnreachableError) as excinfo:
        adapter.query(_request())
    assert excinfo.value.reason == "audit_db_integrity_error"
    assert adapter.last_envelope is None
    assert adapter.last_audit_row is None


def test_connection_in_a_transaction_reports_the_usage_reason(
    audit_conn: sqlite3.Connection,
) -> None:
    """``write_audit_row`` requires an autocommit connection and raises
    ``AuditDbUsageError`` otherwise — a caller-contract bug, not a disk fault.

    It is a plain ``RuntimeError`` subclass, so without its own handler it
    lands in the catch-all fence and is misreported as a write failure,
    pointing an operator at the disk instead of at the caller.
    """
    audit_conn.execute("BEGIN")
    try:
        adapter = AphelionReadAdapter(
            audit_conn_provider=lambda: audit_conn,
            claim_loader=lambda _r: _supersession_pair(),
        )
        with pytest.raises(AphelionUnreachableError) as excinfo:
            adapter.query(_request())
    finally:
        audit_conn.execute("ROLLBACK")

    assert excinfo.value.reason == "audit_db_usage_error"
