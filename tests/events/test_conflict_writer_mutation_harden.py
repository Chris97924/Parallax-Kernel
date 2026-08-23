"""Mutation-hardening for ``parallax.events.conflict_writer`` (land/20260824 wave 5, S5).

Additive companion to ``tests/events/test_conflict_writer.py``,
``tests/router/test_live_arbitration.py``, ``tests/router/test_dual_read_router.py``,
``tests/router/test_dual_read_result.py``,
``tests/router/test_dual_read_decision_log.py`` and the two dual-read /
live-arbitration mutation-harden files. Forty-five semantic mutants were applied
to a pristine tree one at a time against that whole set.

Tally — applied 45 / killed by the pre-existing suite 26 / killed by the tests
below 19 / equivalent (excluded) 0 / unaddressed 0.

What the existing suite could not see
-------------------------------------
The suite is strong where the module is dangerous: the fail-closed contract
(every exception swallowed, "" returned, the failure counter incremented
first), the row_factory save/restore, the migration, the index. Those mutants
all died. Three families survived, and they share one shape — the tests verify
that the writer is CONSISTENT with itself, never what it actually wrote.

* **Dedup is asserted by round trip, so the key's derivation is invisible.**
  Every canonical_ref test writes twice and asserts the two calls return the
  same event_id. That holds under *any* deterministic derivation:
  primary-first, secondary-first, or always-sentinel all dedup equally well.
  Nothing reads back ``target_id``, so the documented precedence order — and
  the rule that an empty-string id is not a usable ref — is untested.
* **The stored row is only ever read back through ``payload_json``.** The
  columns beside it are the writer-side dedup contract spelled out in
  ``_select_existing_event_id``'s docstring: ``actor='system'``,
  ``target_kind='arbitration_conflict_dedup'``, and the ``rule:`` namespace on
  ``approval_tier`` that the MED-CONFLICT-FIELD-SCHEMA comment says was added
  so a future tie-breaker rule cannot collide with old rows. None of the three
  is asserted anywhere.
* **The clock is never inspected, only used.** ``_now_iso_from_us`` decides the
  timezone, the precision and the unit of every ``created_at`` this module
  writes, and the dedup SELECT compares those strings lexicographically — so a
  local-time render, a seconds-precision render, a dropped microsecond
  remainder and a milliseconds-instead-of-microseconds wall clock are all
  invisible to a test that writes and reads with the same function.

Also closed here: the dedup SELECT's ``ORDER BY ... DESC`` (which of two
in-window rows is returned), the inclusive ``>=`` window boundary, the two
malformed-row paths that must NOT be read as a dedup hit, the full-width
event_id, and the ``and uid`` guard that stops an empty payload user_id from
displacing the system sentinel.

Expected values are literals throughout — 3600, ``"__system__"``,
``"rule:source-level"``, and hand-computed ISO-8601 strings for a fixed epoch
microsecond value. Rendering the expected timestamp with the module's own
``_now_iso_from_us`` is exactly what makes that function untestable.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from collections.abc import Iterator
from typing import Any

import pytest

from parallax.events.conflict_writer import (
    CONFLICT_EVENT_SYSTEM_USER_ID,
    DEDUP_WINDOW_SECONDS,
    write_conflict_event,
)
from parallax.migrations import migrate_to_latest
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.live_arbitration import LiveArbitrationDecision
from parallax.router.types import QueryType
from parallax.sqlite_store import connect

# 2026-01-01T00:00:00.000000+00:00 expressed in microseconds since the epoch.
# Hand-computed so every expected ISO string below is a literal rather than a
# render of the function under test.
T0_US = 1_767_225_600_000_000
T0_ISO = "2026-01-01T00:00:00.000000+00:00"
ONE_HOUR_US = 3_600_000_000


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "conflict_writer_harden.db"
    c = connect(db)
    migrate_to_latest(c)
    try:
        yield c
    finally:
        c.close()


def _evidence(*ids: str) -> RetrievalEvidence:
    return RetrievalEvidence(
        hits=tuple({"id": i, "kind": "memory", "score": 1.0} for i in ids),
        stages=("test",),
    )


def _decision(
    *, correlation_id: str = "cid-1", tie_breaker_rule: str = "source-level"
) -> LiveArbitrationDecision:
    return LiveArbitrationDecision(
        winning_source="fallback",  # type: ignore[arg-type]
        tie_breaker_rule=tie_breaker_rule,
        conflict_event_id=None,
        policy_version="v0.3.0-rc",
        correlation_id=correlation_id,
        query_type=QueryType.RECENT_CONTEXT,
        reason_code="source-level/recent_context/fallback",
        decided_at_us_utc=T0_US,
    )


def _row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row is not None, f"event {event_id!r} not in DB"
    return row


def _envelope(conn: sqlite3.Connection, event_id: str) -> dict[str, Any]:
    return json.loads(_row(conn, event_id)["payload_json"])


def _conflict_rows(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'arbitration_conflict'"
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# canonical_ref derivation — WHICH id, not merely a stable one
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonical_ref_is_the_primary_side_first_hit(conn: sqlite3.Connection) -> None:
    """Primary wins when it has a usable id. Read back, not round-tripped.

    Every existing derivation test writes twice and asserts the two calls
    return the same event_id — which is true under primary-first,
    secondary-first and always-sentinel alike, because all three are
    deterministic. The precedence itself is only observable in the stored
    ``target_id``, which nothing reads.

    It matters because ``canonical_ref`` is the operator's handle on the
    conflict: deduping on the secondary's id groups conflicts by the answer
    Aphelion gave rather than by the record the canonical store holds.
    """
    payload = {
        "primary": _evidence("PRIM-1", "PRIM-2"),
        "secondary": _evidence("SEC-1"),
        "user_id": "u1",
    }

    event_id = write_conflict_event(_decision(), payload, conn, now_us_utc=T0_US)

    assert _row(conn, event_id)["target_id"] == "PRIM-1"


@pytest.mark.unit
def test_secondary_is_used_only_when_primary_has_no_usable_id(
    conn: sqlite3.Connection,
) -> None:
    """No hits and an empty-string id are both "no usable ref".

    The ``and ref`` half of the guard is what makes ``""`` fall through rather
    than become the dedup key. An empty canonical_ref is worse than the
    sentinel: it is indistinguishable from a genuine ref in the column, and it
    silently merges every conflict whose primary returned a blank id into one
    dedup bucket, so all but the first are dropped for an hour.
    """
    no_hits = write_conflict_event(
        _decision(correlation_id="c-a"),
        {"primary": _evidence(), "secondary": _evidence("SEC-A")},
        conn,
        now_us_utc=T0_US,
    )
    assert _row(conn, no_hits)["target_id"] == "SEC-A"

    blank_id = write_conflict_event(
        _decision(correlation_id="c-b"),
        {"primary": _evidence(""), "secondary": _evidence("SEC-B")},
        conn,
        now_us_utc=T0_US,
    )
    assert _row(conn, blank_id)["target_id"] == "SEC-B"


# ---------------------------------------------------------------------------
# The stored row: actor, dedup marker, namespaced dedup key
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_row_is_attributed_to_the_system_actor_under_the_dedup_marker(
    conn: sqlite3.Connection,
) -> None:
    """``actor='system'`` and ``target_kind='arbitration_conflict_dedup'``.

    These two columns are how an operator separates machine-emitted rows from
    user-emitted ones and how the dedup SELECT finds its own rows. The marker
    is the writer-side contract documented in ``_select_existing_event_id``;
    changing it silently disables dedup, because the next lookup no longer
    matches anything and every conflict inserts a fresh row.
    """
    event_id = write_conflict_event(
        _decision(), {"primary": _evidence("P")}, conn, now_us_utc=T0_US
    )
    row = _row(conn, event_id)

    assert row["actor"] == "system"
    assert row["target_kind"] == "arbitration_conflict_dedup"
    assert row["event_type"] == "arbitration_conflict"


@pytest.mark.unit
def test_the_dedup_key_carries_the_rule_namespace_prefix(
    conn: sqlite3.Connection,
) -> None:
    """``approval_tier`` stores ``rule:<name>``, not the bare rule name.

    MED-CONFLICT-FIELD-SCHEMA added the prefix so a future ``tie_breaker_rule``
    writes a visibly distinct dedup key and cannot collide with rows written
    under the old bare-string layout. Dropping it is invisible to a round-trip
    test — both writes use the same spelling, so they still dedup with each
    other — and only shows up as silent cross-rule merging against historical
    rows.
    """
    event_id = write_conflict_event(
        _decision(tie_breaker_rule="source-level"),
        {"primary": _evidence("P")},
        conn,
        now_us_utc=T0_US,
    )

    assert _row(conn, event_id)["approval_tier"] == "rule:source-level"


@pytest.mark.unit
def test_an_empty_payload_user_id_does_not_displace_the_system_sentinel(
    conn: sqlite3.Connection,
) -> None:
    """``and uid`` is what stops ``""`` overriding ``__system__``.

    The sentinel exists so operators can grep ``user_id == "__system__"`` to
    isolate machine-emitted rows. An empty string passes the isinstance check,
    fails that grep, and matches nothing else either — the row becomes
    invisible to both queries.
    """
    with_blank = write_conflict_event(
        _decision(correlation_id="c-blank"),
        {"primary": _evidence("P-blank"), "user_id": ""},
        conn,
        now_us_utc=T0_US,
    )
    assert _row(conn, with_blank)["user_id"] == "__system__"
    assert CONFLICT_EVENT_SYSTEM_USER_ID == "__system__"

    with_real = write_conflict_event(
        _decision(correlation_id="c-real"),
        {"primary": _evidence("P-real"), "user_id": "u1"},
        conn,
        now_us_utc=T0_US,
    )
    assert _row(conn, with_real)["user_id"] == "u1"


@pytest.mark.unit
def test_event_ids_are_full_width_uuid4_hex(conn: sqlite3.Connection) -> None:
    """32 hex characters. Truncation is a collision risk, not a cosmetic change.

    ``event_id`` is the events table's primary key and the value the writer
    hands back to the dual-read router as the conflict's handle. Eight hex
    characters is ~4 billion values, which birthday-collides in the tens of
    thousands of rows — well inside one busy day — and a collision surfaces as
    an INSERT failure the writer swallows, returning "".
    """
    event_id = write_conflict_event(
        _decision(), {"primary": _evidence("P")}, conn, now_us_utc=T0_US
    )

    assert len(event_id) == 32
    assert all(c in "0123456789abcdef" for c in event_id)


# ---------------------------------------------------------------------------
# The clock: unit, timezone, precision
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_created_at_is_utc_with_full_microsecond_precision(
    conn: sqlite3.Connection,
) -> None:
    """One fixed input, one literal expected string.

    ``created_at`` is what the dedup SELECT compares lexicographically against
    the window start, so its timezone and precision are load-bearing rather
    than presentational: a local-time render makes the comparison wrong by the
    UTC offset (eight hours here), and truncating to whole seconds collapses
    every sub-second write onto one sortable value. Both are invisible to a
    test that writes and reads through the same renderer, which is why the
    expected value below is hand-computed rather than produced by
    ``_now_iso_from_us``.
    """
    event_id = write_conflict_event(
        _decision(),
        {"primary": _evidence("P")},
        conn,
        now_us_utc=T0_US + 123_456,
    )

    assert _row(conn, event_id)["created_at"] == "2026-01-01T00:00:00.123456+00:00"
    assert _envelope(conn, event_id)["timestamp_us_utc"] == T0_US + 123_456


@pytest.mark.unit
def test_an_explicit_zero_timestamp_is_honoured_not_replaced_by_the_wall_clock(
    conn: sqlite3.Connection,
) -> None:
    """``now_us_utc=0`` is a value, not an absent argument.

    The distinction is ``is not None`` versus truthiness. Under ``or`` the one
    falsy microsecond value in the domain silently becomes "use the wall
    clock", which is the classic sentinel bug — and the epoch is precisely the
    value a replay or a fixed-clock test harness supplies.
    """
    event_id = write_conflict_event(
        _decision(), {"primary": _evidence("P")}, conn, now_us_utc=0
    )

    assert _row(conn, event_id)["created_at"] == "1970-01-01T00:00:00.000000+00:00"
    assert _envelope(conn, event_id)["timestamp_us_utc"] == 0


@pytest.mark.unit
def test_the_default_clock_is_microseconds_since_the_epoch(
    conn: sqlite3.Connection,
) -> None:
    """With no override the stamp must be *now*, in microseconds.

    ``time.time_ns() // 1_000`` is microseconds; ``// 1_000_000`` is
    milliseconds, and feeding milliseconds to a microsecond consumer dates
    every row to 1970 while still producing a well-formed timestamp and a
    working dedup window. The bound is generous (a minute) because the point
    is the UNIT, not the precision of the comparison.
    """
    before = time.time_ns() // 1_000
    event_id = write_conflict_event(_decision(), {"primary": _evidence("P")}, conn)
    after = time.time_ns() // 1_000

    stamped = _envelope(conn, event_id)["timestamp_us_utc"]

    assert before - 60_000_000 <= stamped <= after + 60_000_000, stamped
    assert _row(conn, event_id)["created_at"].startswith("20")


# ---------------------------------------------------------------------------
# The dedup window
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_dedup_window_is_one_hour_and_its_far_edge_is_inclusive(
    conn: sqlite3.Connection,
) -> None:
    """Exactly one hour later still dedups; one microsecond past it does not.

    Two mutants meet on this boundary and neither is visible to a test that
    picks its offsets from ``DEDUP_WINDOW_SECONDS``: narrowing the window (any
    smaller value passes a test that only checks "much later inserts a new
    row"), and tightening the re-check from ``>=`` to ``>`` (which only differs
    for a record landing exactly on the edge). The offsets below are literal
    microseconds.
    """
    assert DEDUP_WINDOW_SECONDS == 3600

    payload = {"primary": _evidence("EDGE"), "user_id": "u1"}
    first = write_conflict_event(
        _decision(correlation_id="c1"), payload, conn, now_us_utc=T0_US
    )

    on_the_edge = write_conflict_event(
        _decision(correlation_id="c2"), payload, conn, now_us_utc=T0_US + ONE_HOUR_US
    )
    assert on_the_edge == first, "a record exactly one hour old is still in-window"
    assert _conflict_rows(conn) == 1

    past_the_edge = write_conflict_event(
        _decision(correlation_id="c3"), payload, conn, now_us_utc=T0_US + ONE_HOUR_US + 1
    )
    assert past_the_edge != first
    assert _conflict_rows(conn) == 2


@pytest.mark.unit
def test_dedup_returns_the_newest_in_window_row_not_the_oldest(
    conn: sqlite3.Connection,
) -> None:
    """``ORDER BY created_at DESC`` — the caller gets the live conflict.

    Two rows can share a dedup key inside one window (a row written before the
    marker was in use, a replay, a manual insert). Returning the oldest hands
    the router a handle to a stale conflict while the recent one goes
    unreferenced, and every existing test has exactly one candidate row, under
    which DESC and ASC are indistinguishable.
    """
    payload = {"primary": _evidence("ORDER"), "user_id": "u1"}
    older = write_conflict_event(
        _decision(correlation_id="c-old"), payload, conn, now_us_utc=T0_US
    )

    newer = "newer-row-id"
    conn.execute(
        "INSERT INTO events "
        "(event_id, user_id, actor, event_type, target_kind, target_id, "
        " payload_json, approval_tier, created_at, session_id) "
        "VALUES (?, '__system__', 'system', 'arbitration_conflict', "
        "        'arbitration_conflict_dedup', 'ORDER', ?, 'rule:source-level', ?, NULL)",
        (
            newer,
            json.dumps({"timestamp_us_utc": T0_US + 10_000_000}),
            "2026-01-01T00:00:10.000000+00:00",
        ),
    )
    conn.commit()

    hit = write_conflict_event(
        _decision(correlation_id="c-new"), payload, conn, now_us_utc=T0_US + 20_000_000
    )

    assert hit == newer
    assert hit != older


@pytest.mark.unit
def test_a_row_whose_payload_timestamp_is_not_an_integer_is_not_a_dedup_hit(
    conn: sqlite3.Connection,
) -> None:
    """An unreadable stamp means "cannot confirm in-window", so write a new row.

    The SQL filter is on ``created_at``; the payload re-check exists because
    that column's legacy ISO format can drift from the envelope's microsecond
    clock. When the re-check cannot be performed the safe answer is to insert —
    a duplicate row is recoverable, whereas treating an unverifiable row as a
    hit drops a real conflict for an hour and returns an event_id pointing at
    the wrong record.
    """
    conn.execute(
        "INSERT INTO events "
        "(event_id, user_id, actor, event_type, target_kind, target_id, "
        " payload_json, approval_tier, created_at, session_id) "
        "VALUES ('bad-ts', '__system__', 'system', 'arbitration_conflict', "
        "        'arbitration_conflict_dedup', 'BADTS', ?, 'rule:source-level', ?, NULL)",
        (json.dumps({"timestamp_us_utc": "not-an-int"}), T0_ISO),
    )
    conn.commit()

    written = write_conflict_event(
        _decision(),
        {"primary": _evidence("BADTS"), "user_id": "u1"},
        conn,
        now_us_utc=T0_US + 1_000_000,
    )

    assert written != ""
    assert written != "bad-ts"
    assert _conflict_rows(conn) == 2


@pytest.mark.unit
def test_a_row_with_unparseable_payload_json_is_not_a_dedup_hit(
    conn: sqlite3.Connection,
) -> None:
    """Same rule for a corrupt envelope: fail towards writing, not towards silence."""
    conn.execute(
        "INSERT INTO events "
        "(event_id, user_id, actor, event_type, target_kind, target_id, "
        " payload_json, approval_tier, created_at, session_id) "
        "VALUES ('bad-json', '__system__', 'system', 'arbitration_conflict', "
        "        'arbitration_conflict_dedup', 'BADJSON', ?, 'rule:source-level', ?, NULL)",
        ("{not valid json", T0_ISO),
    )
    conn.commit()

    written = write_conflict_event(
        _decision(),
        {"primary": _evidence("BADJSON"), "user_id": "u1"},
        conn,
        now_us_utc=T0_US + 1_000_000,
    )

    assert written != ""
    assert written != "bad-json"
    assert _conflict_rows(conn) == 2
