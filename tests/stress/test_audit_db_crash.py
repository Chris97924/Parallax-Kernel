"""Crash-recovery stress for parallax.apex.audit_db.

Two scenarios:

* Mid-write crash: child process writes rows in a tight loop printing
  ``ok i`` after each commit. Parent reads stdout until at least 100
  commits land (forces WAL frame boundary crossing — default
  ``wal_autocheckpoint=200`` pages plus our test's 200 pages of WAL
  buffer mean checkpoints fire during the run, NOT after termination),
  then ``terminate()`` s. Parent reopens and asserts:
    - ALL stdout is drained (no stuck pipe).
    - ``count == max_committed + 1`` exactly — every acknowledged row
      survives, no uncommitted row sneaks in.
    - schema CHECK + UNIQUE constraints still hold (no torn write).
    - ``PRAGMA integrity_check`` returns ``'ok'`` (no half-page).

* Mid-bootstrap crash: child opens audit_db (which runs
  ``CREATE TABLE IF NOT EXISTS`` + schema_version INSERT OR IGNORE)
  and idles. Parent terminates after the child confirms bootstrap.
  Parent re-opens — bootstrap must be idempotent and succeed.
"""

from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.integration

_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_MIN_COMMITTED_BEFORE_KILL = 100  # ≥ ~1 WAL frame batch; exercises recovery


def _child_writer_script(db_path: pathlib.Path) -> str:
    """Child bootstraps + writes rows as fast as possible.

    Each ``ok i`` line prints AFTER ``write_audit_row`` returns from a
    successful COMMIT. Therefore parent's ``max(ok i)`` is the exact
    high-water mark of acknowledged commits. Rows are written in order
    starting at i=0, so rows 0..max are all committed; any further row
    (max+1 or beyond) either committed before COMMIT returned or did
    not — and the assertion ``count == max+1`` is the strict atomicity
    check that catches partial-commit bugs.
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


def _drain_remaining_ok_lines(stream) -> int:
    """Read any remaining ``ok i`` lines after terminate() returns.

    Returns the max ``i`` seen across the entire stream (or -1 if none).
    """
    high = -1
    if stream is None:
        return high
    while True:
        line = stream.readline()
        if not line:
            break
        if line.startswith("ok "):
            try:
                high = max(high, int(line.split()[1]))
            except (ValueError, IndexError):
                pass
    return high


class TestCrashMidWrite:
    def test_committed_rows_survive_terminate_exact_count(
        self, tmp_path: pathlib.Path
    ) -> None:
        db = tmp_path / "audit.db"
        script = tmp_path / "child_writer.py"
        script.write_text(_child_writer_script(db), encoding="utf-8")

        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        assert proc.stdout is not None

        max_seen = -1
        deadline = time.time() + 30.0
        while time.time() < deadline and max_seen < (_MIN_COMMITTED_BEFORE_KILL - 1):
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
        # MAX of in-band + post-terminate buffered rows.
        max_post_drain = _drain_remaining_ok_lines(proc.stdout)
        max_committed = max(max_seen, max_post_drain)
        stderr_dump = proc.stderr.read() if proc.stderr else ""

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

            # No CHECK violation hiding in WAL replay.
            bogus = conn.execute(
                "SELECT COUNT(*) FROM audit_row "
                "WHERE outcome NOT IN ('hit','miss','divergence','error') "
                "OR source NOT IN ('aphelion','parallax')"
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
