"""Mutation-hardening for ``parallax.canary.outcomes`` (overnight-20260816 S6).

New test file — ``OutcomeStore`` had no direct tests at all. Every existing
exercise of it comes through ``tests/dod/`` and the canary exporter, which use
it as a fixture: they write well-formed rows and then assert on what the DoD
verifier computed. That leaves the store's own stated hard invariants (module
docstring, "Hard invariants") unpinned, and each test below was written against
a semantic mutant that survived the suite because of it:

  * "``stage`` is enumerated — invalid values raise on insert" and the same for
    ``outcome``: the two ``pytest.raises(ValueError, match="Unknown canary
    stage")`` cases in the suite reach ``dod.compute_dod`` and
    ``dod_prometheus``, each of which re-checks ``KNOWN_STAGES`` itself. Both
    stayed green with ``OutcomeStore.record``'s own gates removed, so an
    unenumerated stage could enter the table through any other caller.
  * "Re-recording the same ``event_id`` is idempotent (UPSERT) so canary retries
    don't double-count": no test ever re-records an id, so degrading both
    UPSERTs to ``DO NOTHING`` — a retry silently keeping the stale outcome —
    changed nothing.
  * ``record`` returns a bool that the callers branch on; the failure path
    returning ``True`` was invisible because no test ever made a write fail.
  * ``iter_stage``'s documented ascending order and its own stage gate had no
    assertions.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from parallax.canary.audit_log import AUDIT_DB_ENV
from parallax.canary.outcomes import KNOWN_OUTCOMES, KNOWN_STAGES, OutcomeStore

_STAGE = "m4_1pct"


@pytest.fixture
def store(tmp_path: Path):
    s = OutcomeStore(db_path=tmp_path / "canary_audit.db")
    yield s
    s.close()


def _row_count(store: OutcomeStore) -> int:
    return store._connect().execute("SELECT COUNT(*) FROM canary_outcomes").fetchone()[0]


# ===========================================================================
# Enumeration gates (module docstring, "Hard invariants")
# ===========================================================================


@pytest.mark.unit
class TestEnumerationIsEnforcedOnInsert:
    def test_unknown_stage_raises_and_writes_nothing(self, store: OutcomeStore) -> None:
        with pytest.raises(ValueError, match="Unknown canary stage"):
            store.record(event_id="evt-bad-stage", stage="m4_99pct", outcome="ok")
        assert store.lookup("evt-bad-stage") is None
        assert _row_count(store) == 0

    def test_unknown_outcome_raises_and_writes_nothing(self, store: OutcomeStore) -> None:
        with pytest.raises(ValueError, match="Unknown canary outcome"):
            store.record(event_id="evt-bad-outcome", stage=_STAGE, outcome="catastrophe")
        assert store.lookup("evt-bad-outcome") is None
        assert _row_count(store) == 0

    def test_stage_is_checked_before_outcome(self, store: OutcomeStore) -> None:
        """Both bad → the stage message is the one raised (deterministic error)."""
        with pytest.raises(ValueError, match="Unknown canary stage"):
            store.record(event_id="evt-both-bad", stage="nope", outcome="nope")

    @pytest.mark.parametrize("stage", sorted(KNOWN_STAGES))
    def test_every_known_stage_is_accepted(self, store: OutcomeStore, stage: str) -> None:
        assert store.record(event_id=f"evt-{stage}", stage=stage, outcome="ok") is True

    @pytest.mark.parametrize("outcome", sorted(KNOWN_OUTCOMES))
    def test_every_known_outcome_is_accepted(self, store: OutcomeStore, outcome: str) -> None:
        assert store.record(event_id=f"evt-{outcome}", stage=_STAGE, outcome=outcome) is True

    def test_iter_stage_rejects_an_unknown_stage(self, store: OutcomeStore) -> None:
        """The read side gates too — an unknown stage is an error, not an empty result.

        ``iter_stage`` is a generator, so the guard only fires once the caller
        draws from it; silently yielding nothing would read as "this stage has
        no outcomes yet", which for a DoD query is the opposite of the truth.
        """
        with pytest.raises(ValueError, match="Unknown canary stage"):
            list(store.iter_stage("m4_99pct"))


# ===========================================================================
# Retry idempotency (module docstring, "Hard invariants")
# ===========================================================================


@pytest.mark.unit
class TestReRecordIsAnUpsert:
    def test_re_record_updates_stage_and_outcome_in_place(self, store: OutcomeStore) -> None:
        """A retry that carries a corrected verdict must overwrite, not be dropped.

        ``DO NOTHING`` would keep the row count at one — the visible half of
        idempotency — while silently preserving the stale ``ok`` after the
        canary decided the event was a discrepancy.
        """
        store.record(event_id="evt-retry", stage=_STAGE, outcome="ok")
        store.record(event_id="evt-retry", stage="m4_10pct", outcome="discrepancy")

        record = store.lookup("evt-retry")
        assert record is not None
        assert (record.stage, record.outcome) == ("m4_10pct", "discrepancy")
        assert _row_count(store) == 1  # idempotent: still exactly one row

    def test_re_record_with_timestamp_updates_all_three_columns(
        self, store: OutcomeStore
    ) -> None:
        store.record(
            event_id="evt-retry-ts",
            stage=_STAGE,
            outcome="ok",
            recorded_at="2026-08-16T00:00:00.000Z",
        )
        store.record(
            event_id="evt-retry-ts",
            stage="m4_50pct",
            outcome="data_loss",
            recorded_at="2026-08-16T01:00:00.000Z",
        )

        record = store.lookup("evt-retry-ts")
        assert record is not None
        assert (record.stage, record.outcome, record.recorded_at) == (
            "m4_50pct",
            "data_loss",
            "2026-08-16T01:00:00.000Z",
        )
        assert _row_count(store) == 1

    def test_distinct_event_ids_are_separate_rows(self, store: OutcomeStore) -> None:
        """Positive twin: the UPSERT must not collapse different events."""
        store.record(event_id="evt-a", stage=_STAGE, outcome="ok")
        store.record(event_id="evt-b", stage=_STAGE, outcome="discrepancy")
        assert _row_count(store) == 2


# ===========================================================================
# Write-failure reporting
# ===========================================================================


@pytest.mark.unit
class TestRecordReportsWriteFailure:
    def test_failed_write_returns_false_and_logs(
        self, store: OutcomeStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A write that did not land must report ``False`` — never a bare success.

        ``record`` is spec'd to swallow the SQLite error rather than take the
        canary handler down with it, which makes the return value the *only*
        signal that a DoD row is missing. Returning ``True`` from the except
        branch turns a lost outcome row into a silent one.
        """
        store._connect().close()  # the cached connection is now unusable

        with caplog.at_level(logging.WARNING, logger="parallax.canary.outcomes"):
            assert store.record(event_id="evt-doomed", stage=_STAGE, outcome="ok") is False

        assert any("record_failed" in rec.getMessage() for rec in caplog.records)

    def test_successful_write_returns_true(self, store: OutcomeStore) -> None:
        """Positive twin: the same return value must distinguish the two cases."""
        assert store.record(event_id="evt-fine", stage=_STAGE, outcome="ok") is True


# ===========================================================================
# Read path
# ===========================================================================


@pytest.mark.unit
class TestIterStageOrdering:
    def test_rows_come_back_oldest_first(self, store: OutcomeStore) -> None:
        """``iter_stage`` is documented as ascending ``recorded_at``.

        The rows are inserted out of chronological order so insertion order and
        ``event_id`` order both differ from the expected result — otherwise a
        reversed (or absent) ORDER BY could still produce the right sequence by
        accident.
        """
        for event_id, when in (
            ("evt-mid", "2026-08-16T12:00:00.000Z"),
            ("evt-late", "2026-08-16T18:00:00.000Z"),
            ("evt-early", "2026-08-16T06:00:00.000Z"),
        ):
            store.record(event_id=event_id, stage=_STAGE, outcome="ok", recorded_at=when)

        assert [r.event_id for r in store.iter_stage(_STAGE)] == [
            "evt-early",
            "evt-mid",
            "evt-late",
        ]

    def test_iter_stage_filters_by_stage(self, store: OutcomeStore) -> None:
        store.record(event_id="evt-1pct", stage="m4_1pct", outcome="ok")
        store.record(event_id="evt-10pct", stage="m4_10pct", outcome="ok")
        assert [r.event_id for r in store.iter_stage("m4_10pct")] == ["evt-10pct"]

    def test_lookup_projects_every_column(self, store: OutcomeStore) -> None:
        store.record(
            event_id="evt-proj",
            stage="m4_50pct",
            outcome="discrepancy",
            recorded_at="2026-08-16T09:30:00.000Z",
        )
        record = store.lookup("evt-proj")
        assert record is not None
        assert (record.event_id, record.stage, record.outcome, record.recorded_at) == (
            "evt-proj",
            "m4_50pct",
            "discrepancy",
            "2026-08-16T09:30:00.000Z",
        )


# ===========================================================================
# Construction / connection handling
# ===========================================================================


@pytest.mark.unit
class TestStoreConstruction:
    def test_explicit_db_path_wins_over_the_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``db_path=`` is the caller's override, not a fallback.

        Every existing test either passes a path or sets the env var, never
        both, so reversing the precedence was invisible — and a store that
        quietly writes to the ambient env path instead of the one it was handed
        puts DoD rows in a different file from the audit rows they join to.
        """
        env_db = tmp_path / "from_env.db"
        explicit_db = tmp_path / "explicit.db"
        monkeypatch.setenv(AUDIT_DB_ENV, str(env_db))

        store = OutcomeStore(db_path=explicit_db)
        try:
            assert store.db_path == explicit_db
            store.record(event_id="evt-path", stage=_STAGE, outcome="ok")
        finally:
            store.close()
        assert explicit_db.exists()
        assert not env_db.exists()

    def test_missing_parent_directory_is_created(self, tmp_path: Path) -> None:
        """The store creates its own parent chain rather than failing to open."""
        nested = tmp_path / "does" / "not" / "exist" / "canary_audit.db"
        store = OutcomeStore(db_path=nested)
        try:
            assert store.record(event_id="evt-nested", stage=_STAGE, outcome="ok") is True
        finally:
            store.close()
        assert nested.exists()

    def test_each_thread_gets_its_own_connection(self, store: OutcomeStore) -> None:
        """The per-thread cache is what keeps multi-thread canary handlers working.

        Handing a second thread the first thread's connection trips SQLite's
        ``check_same_thread`` guard, which ``record`` catches — so the write is
        reported as failed rather than crashing, and nothing in the suite
        noticed because no test wrote from another thread.
        """
        store.record(event_id="evt-main", stage=_STAGE, outcome="ok")
        results: list[object] = []

        def _write_from_thread() -> None:
            results.append(store.record(event_id="evt-worker", stage=_STAGE, outcome="ok"))

        worker = threading.Thread(target=_write_from_thread)
        worker.start()
        worker.join(timeout=30)

        assert results == [True]
        assert store.lookup("evt-worker") is not None

    def test_writes_are_visible_to_a_second_connection(self, tmp_path: Path) -> None:
        """Autocommit: a recorded row is durable immediately, not held in a txn.

        The canary writer and the DoD reader are separate processes in
        production; a row still sitting in an open transaction is invisible to
        the reader and is lost outright when the writer exits.
        """
        db = tmp_path / "shared.db"
        writer = OutcomeStore(db_path=db)
        try:
            writer.record(event_id="evt-shared", stage=_STAGE, outcome="ok")
            raw = sqlite3.connect(db, timeout=5.0)
            try:
                row = raw.execute(
                    "SELECT stage, outcome FROM canary_outcomes WHERE event_id = ?",
                    ("evt-shared",),
                ).fetchone()
            finally:
                raw.close()
            assert row == (_STAGE, "ok")
        finally:
            writer.close()
