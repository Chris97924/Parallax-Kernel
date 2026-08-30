"""Crash-recovery stress for parallax.apex.audit_db.

Two scenarios:

* Mid-write crash: child process writes rows one at a time, each
  gated by an explicit ``go``/``ok`` handshake over stdin/stdout (see
  ``_child_writer_script``). Parent drives the handshake until at
  least 100 commits land, then withholds the next ``go`` and
  ``terminate()`` s — at that instant the child is guaranteed to be
  parked in its stdin ``readline()``, not mid-commit or mid-print, so
  the parent's count of acknowledged commits is exact by construction
  (see ``_child_writer_script`` docstring for why a free-running child
  could not offer that guarantee). The recovery path actually
  exercised is WAL replay on reopen — 100 small rows do NOT generate
  enough WAL pages (~20-25 KB) to cross the
  ``wal_autocheckpoint=200`` pages (~1.6 MB) threshold mid-run, so
  checkpoints fire at close time, not during the run. Parent reopens
  and asserts:
    - ALL stdout is drained (no stuck pipe).
    - ``count == max_committed + 1`` exactly — every acknowledged row
      survives, no uncommitted row sneaks in.
    - schema CHECK + UNIQUE constraints still hold (no torn write).
    - ``PRAGMA integrity_check`` returns ``'ok'`` (no half-page).

* Mid-bootstrap crash: child opens audit_db (which runs
  ``CREATE TABLE IF NOT EXISTS`` + schema_version INSERT OR IGNORE)
  and idles. Parent terminates after the child confirms bootstrap.
  Parent re-opens — bootstrap must be idempotent and succeed. This
  scenario is a single one-shot event (bootstrap, then idle) with no
  repeated commit/observe cycle, so it is not subject to the race
  described above and needs no handshake.
"""

from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from parallax.apex.audit_writer import OUTCOME_VALUES, SOURCE_VALUES

pytestmark = pytest.mark.integration

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_MIN_COMMITTED_BEFORE_KILL = 100  # ≥ ~1 WAL frame batch; exercises recovery


def _child_writer_script(db_path: pathlib.Path) -> str:
    """Child writes one row per ``go`` line read from stdin.

    Each ``ok i`` line prints AFTER ``write_audit_row`` returns from a
    successful COMMIT — so far this matches the original design.
    What changed: the child no longer free-runs. It blocks in
    ``sys.stdin.readline()`` *before* starting each row, and only
    begins the next commit once the parent has sent another ``go``.

    Why this was necessary: the previous version wrote rows in a tight
    loop with no gate, relying on the parent's read-then-terminate
    timing to bound the count. But COMMIT-durable and
    print-flushed-to-the-pipe are two independent OS-level events with
    a real (if usually tiny) gap between them — a child that gets
    preempted after ``write_audit_row`` returns but before the
    ``sys.stdout.write``/``flush`` for that row's ``ok i`` line can be
    ``terminate()``-d in that gap: the row is durably committed but
    its acknowledgment never reached the parent. Under an idle machine
    this window is sub-millisecond and essentially never observed
    (25/25 clean runs locally); under load (verified by running 16
    CPU-bound busy-loops on this 20-core host alongside the test, 30
    iterations) it reproduced in 12/30 runs, always as ``count ==
    max_committed + 2`` — exactly one extra committed-but-unannounced
    row, consistent with the single-threaded child having at most one
    row "in flight" across that gap at kill time.

    The handshake removes the gap from the parent's decision entirely:
    the child cannot start row i+1's commit until it has read a fresh
    ``go``, so once the parent has read row i's ``ok`` line and simply
    withholds the next ``go``, the child is provably parked — not
    mid-commit, not mid-print — before the parent calls
    ``terminate()``. Rows are written in order starting at i=0, so
    rows 0..max are all committed; the assertion ``count == max+1`` is
    the strict atomicity check that catches partial-commit bugs.
    """
    return textwrap.dedent(
        f"""
        import sys, uuid, pathlib
        sys.path.insert(0, r"{_ROOT}")
        from parallax.apex.audit_db import open_audit_db, write_audit_row
        from parallax.apex.audit_writer import canonicalize_row

        db = pathlib.Path(r"{db_path}")
        conn = open_audit_db(db, validate=False)
        try:
            i = 0
            while True:
                # Deterministic handshake: block here until the parent
                # sends an explicit go-ahead. The parent only advances
                # this once it has read the previous row's "ok" line,
                # so this child is never mid-commit / mid-print at the
                # instant the parent decides to stop sending "go" and
                # terminates instead.
                signal = sys.stdin.readline()
                if not signal:
                    break
                row = canonicalize_row({{
                    "claim_id": f"0193e2b1-0001-7000-8000-{{i:012x}}",
                    "envelope_message_id": str(uuid.uuid4()),
                    "outcome": "hit",
                    "package_id": f"0193ef00-0001-7000-8000-{{i:012x}}",
                    "session_id": f"sess-crash-{{i}}",
                    "signer_id": "crash@aphelion-graph",
                    "signer_manifest_digest": "9" * 64,
                    "source": "aphelion",
                    "ts": "2026-05-09T14:23:11Z",
                }})
                write_audit_row(conn, row)
                sys.stdout.write(f"ok {{i}}\\n")
                sys.stdout.flush()
                i += 1
        finally:
            conn.close()
        """
    )


def _child_bootstrap_only_script(db_path: pathlib.Path) -> str:
    return textwrap.dedent(
        f"""
        import sys, pathlib, time
        sys.path.insert(0, r"{_ROOT}")
        from parallax.apex.audit_db import open_audit_db

        db = pathlib.Path(r"{db_path}")
        conn = open_audit_db(db, validate=False)
        try:
            sys.stdout.write("bootstrapped\\n")
            sys.stdout.flush()
            time.sleep(60)  # idle until parent terminates
        finally:
            conn.close()
        """
    )


def _drain_remaining_ok_lines(stream) -> tuple[int, list[str]]:
    """Read any remaining ``ok i`` lines after terminate() returns.

    Returns ``(max_i, unparseable)`` where ``max_i`` is the highest
    sequence number seen (or -1 if none) and ``unparseable`` collects
    any non-``ok`` lines (e.g. tracebacks from the child) so the
    caller can include them in an assertion-failure diagnostic — a
    silent ``pass`` would mask child crashes as count mismatches.
    """
    high = -1
    unparseable: list[str] = []
    if stream is None:
        return high, unparseable
    while True:
        line = stream.readline()
        if not line:
            break
        if line.startswith("ok "):
            try:
                high = max(high, int(line.split()[1]))
            except (ValueError, IndexError):
                unparseable.append(line.rstrip("\n"))
        else:
            unparseable.append(line.rstrip("\n"))
    return high, unparseable


class TestCrashMidWrite:
    def test_committed_rows_survive_terminate_exact_count(
        self, tmp_path: pathlib.Path
    ) -> None:
        db = tmp_path / "audit.db"
        script = tmp_path / "child_writer.py"
        script.write_text(_child_writer_script(db), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        assert proc.stdout is not None
        assert proc.stdin is not None

        # Deterministic go/ok handshake (see _child_writer_script's
        # docstring for why a free-running child cannot give an exact
        # count under scheduling pressure). The parent drives one
        # commit at a time: send "go", read the resulting "ok i" line,
        # repeat. Once _MIN_COMMITTED_BEFORE_KILL commits are
        # acknowledged, the parent simply stops sending "go" — the
        # child is then guaranteed to be blocked in its stdin
        # readline(), never mid-commit, so terminate() below cannot
        # race an unannounced commit.
        max_seen = -1
        deadline = time.time() + 30.0
        while time.time() < deadline and max_seen < (_MIN_COMMITTED_BEFORE_KILL - 1):
            try:
                proc.stdin.write("go\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                break  # child died; count assertion below reports it with stderr
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("ok "):
                max_seen = int(line.split()[1])

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

        # Drain remaining buffered stdout so the high-water mark is the
        # MAX of in-band + post-terminate buffered rows. With the
        # handshake above this should always be empty (the child never
        # writes without a "go" it did not receive), but the drain is
        # kept as a defensive belt-and-braces check. Capture any
        # non-``ok`` lines (e.g. child-side tracebacks) so a count
        # mismatch surfaces the underlying child error instead of a
        # cryptic numeric diff.
        max_post_drain, drain_noise = _drain_remaining_ok_lines(proc.stdout)
        max_committed = max(max_seen, max_post_drain)
        stderr_dump = proc.stderr.read() if proc.stderr else ""
        if drain_noise:
            stderr_dump = (
                f"{stderr_dump}\n[unparseable stdout lines]\n"
                + "\n".join(drain_noise)
            )

        assert max_committed >= _MIN_COMMITTED_BEFORE_KILL - 1, (
            f"child did not produce enough commits before terminate; "
            f"max_committed={max_committed}, stderr={stderr_dump!r}"
        )

        # Reopen + verify exact count + integrity.
        conn = sqlite3.connect(str(db))
        try:
            conn.row_factory = sqlite3.Row
            (count,) = conn.execute("SELECT COUNT(*) FROM audit_row").fetchone()
            # Strict: every acknowledged commit survived, no extras
            # snuck in via an uncommitted transaction.
            assert count == max_committed + 1, (
                f"crash-survival count mismatch: "
                f"max committed={max_committed} (so expected {max_committed + 1} rows), "
                f"actually found {count}. stderr={stderr_dump!r}"
            )

            # No CHECK violation hiding in WAL replay. Enum literals
            # come from the writer's source-of-truth frozensets so the
            # check stays valid if a new outcome / source value is
            # added to OUTCOME_VALUES / SOURCE_VALUES.
            outcome_list = ",".join(f"'{v}'" for v in sorted(OUTCOME_VALUES))
            source_list = ",".join(f"'{v}'" for v in sorted(SOURCE_VALUES))
            bogus = conn.execute(
                f"SELECT COUNT(*) FROM audit_row "
                f"WHERE outcome NOT IN ({outcome_list}) "
                f"OR source NOT IN ({source_list})"
            ).fetchone()[0]
            assert bogus == 0

            # No torn page / half-applied DDL.
            row = conn.execute("PRAGMA integrity_check").fetchone()
            assert row[0] == "ok"

            # Schema columns intact.
            col_names = {
                r["name"] for r in conn.execute("PRAGMA table_info(audit_row)").fetchall()
            }
            for required in (
                "claim_id",
                "envelope_message_id",
                "outcome",
                "package_id",
                "session_id",
                "signer_id",
                "signer_manifest_digest",
                "source",
                "ts",
            ):
                assert required in col_names

            # UNIQUE on envelope_message_id survived.
            unique_idxs = [
                r for r in conn.execute(
                    "PRAGMA index_list(audit_row)"
                ).fetchall() if r["unique"] == 1
            ]
            assert len(unique_idxs) >= 1
        finally:
            conn.close()


class TestCrashMidBootstrap:
    def test_bootstrap_idempotent_after_terminate(
        self, tmp_path: pathlib.Path
    ) -> None:
        db = tmp_path / "audit.db"
        script = tmp_path / "child_bootstrap.py"
        script.write_text(_child_bootstrap_only_script(db), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None

        deadline = time.time() + 15.0
        bootstrapped = False
        while time.time() < deadline and not bootstrapped:
            line = proc.stdout.readline()
            if not line:
                break
            if line.strip() == "bootstrapped":
                bootstrapped = True

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr_dump = proc.stderr.read() if proc.stderr else ""

        assert bootstrapped, f"child did not bootstrap; stderr={stderr_dump!r}"

        from parallax.apex.audit_db import open_audit_db

        conn = open_audit_db(db, validate=False)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            assert row[0] == "ok"
            cols = conn.execute("PRAGMA table_info(audit_row)").fetchall()
            assert any(r["name"] == "envelope_message_id" for r in cols)
            (vcount,) = conn.execute(
                "SELECT COUNT(*) FROM audit_db_schema_version"
            ).fetchone()
            assert vcount == 1
        finally:
            conn.close()
