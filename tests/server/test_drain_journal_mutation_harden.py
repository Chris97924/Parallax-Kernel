"""Mutation-hardening for ``parallax.server.drain_journal`` (land/20260823 wave 4, S3).

Additive companion to ``tests/server/test_drain_timeout_durable_counter_106.py``,
``tests/server/test_drain_timeout_durable_signal_106.py``,
``tests/server/test_lifespan.py`` and ``tests/server/test_app_t14_wiring.py``.
Twenty-two semantic mutants were applied to a pristine tree one at a time
against that set; eight died on contact and fourteen survived. Every survivor
has a named killer below. Four further tests are marked COMPANION: they close no
mutant of their own and are kept only because they complete a table that reads
wrong half-written — the resolution-order pair and the ``readable`` quartet.

Tally — applied 22 / killed by the pre-existing suites 8 / killed by the tests
below 14 / equivalent (excluded) 0 / unaddressed 0.

The shape of the blind spot
---------------------------
This module carries one fact across a process boundary, and the existing tests
verify it by writing with ``record_drain_timeout`` and reading back with
``read_drain_journal``. That is a round trip through the same two functions, so
anything the two agree on is invisible to it:

* **The on-disk format is never inspected.** ``_LAST_INFLIGHT_KEY``,
  ``DEFAULT_DRAIN_JOURNAL_NAME`` and ``DRAIN_JOURNAL_ENV`` can all be renamed
  and every round-trip test stays green — while the only case that matters, an
  old process writing the file and a new one reading it, silently loses the
  event. The bytes on disk are the actual interface here, so they are asserted
  as bytes.
* **Only well-formed input is ever read back.** ``_coerce_total`` rejects
  booleans and NaN and nothing feeds it either.
* **``readable`` is never told apart from ``total``.** The flag exists so "no
  timeout has happened" can be distinguished from "the record of one was
  lost", and ``total == 0.0`` is identical in both cases — so a test that
  asserts only the total cannot see the flag flip.
* **The durability mechanics are unobserved.** The fsync, the temp-file
  cleanup, and the fail-open posture on an unwritable path are what make this a
  journal rather than a file, and a happy-path round trip passes without any of
  them.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from parallax.server import drain_journal as dj

# ---------------------------------------------------------------------------
# The on-disk format is a cross-process interface, not an implementation detail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_journal_file_uses_the_documented_key_names(tmp_path: Path) -> None:
    """The JSON keys are literals here, never ``dj._TOTAL_KEY``.

    Reader and writer both go through the same constants, so renaming one keeps
    every round-trip test green while breaking the only case the module exists
    for: the process that wrote the file is not the process that reads it. A
    deploy would restart the count from zero and ``DrainTimeoutDetected`` would
    never see the step it is waiting for.
    """
    path = tmp_path / "journal.json"
    dj.record_drain_timeout(inflight_count=3, timeout_seconds=900.0, path=path)

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload == {
        "drain_timeout_total": 1.0,
        "last_inflight_count": 3,
        "last_timeout_seconds": 900.0,
    }


@pytest.mark.unit
def test_default_journal_filename_and_env_var_are_the_documented_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both are operator-facing: a runbook names the file, a unit file sets the env.

    Renaming either is invisible to a test that passes ``path=`` explicitly,
    which is what every existing test does.
    """
    assert dj.DEFAULT_DRAIN_JOURNAL_NAME == "parallax_drain_journal.json"
    assert dj.DRAIN_JOURNAL_ENV == "PARALLAX_DRAIN_JOURNAL_PATH"

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PARALLAX_DRAIN_JOURNAL_PATH", raising=False)
    assert dj.resolve_drain_journal_path().name == "parallax_drain_journal.json"


@pytest.mark.unit
def test_explicit_path_beats_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COMPANION to the test below — resolution order is arg -> env -> cwd default.

    Kept because the empty-env case only makes sense next to the case that
    establishes what the ordering is.
    """
    monkeypatch.setenv("PARALLAX_DRAIN_JOURNAL_PATH", str(tmp_path / "from_env.json"))
    explicit = tmp_path / "from_arg.json"

    assert dj.resolve_drain_journal_path(explicit) == explicit


@pytest.mark.unit
def test_empty_env_var_falls_through_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PARALLAX_DRAIN_JOURNAL_PATH=`` is an unset variable, not ``Path('')``.

    An empty-string export is the ordinary shape of "this deploy does not
    override it" in a templated environment file. Treating it as a value
    resolves the journal to the current directory itself, which is not a
    writable file path — so the drain event is lost with only a WARNING, on
    exactly the deploys that were trying to be explicit about defaults.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PARALLAX_DRAIN_JOURNAL_PATH", "")

    assert dj.resolve_drain_journal_path().name == dj.DEFAULT_DRAIN_JOURNAL_NAME


# ---------------------------------------------------------------------------
# _coerce_total rejects everything a writer could not have produced
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (True, "a JSON true is an int subclass and would coerce to 1.0"),
        (float("nan"), "NaN compares false against every threshold"),
        (-5.0, "a negative total makes the restore delta arithmetic negative"),
        ("7", "a string total is not something the writer emits"),
        (None, "a missing key reads as None"),
    ],
)
def test_unwriteable_total_values_coerce_to_zero(raw: object, why: str) -> None:
    """The whole rejection table; the boolean and NaN rows are the survivors.

    None of these is a value ``record_drain_timeout`` can produce — they reach
    the reader only from a hand-edited or corrupted file — and each breaks a
    different consumer. ``True`` fabricates a drain timeout that never
    happened. NaN is worse: ``delta = nan - current`` is NaN, ``nan <= 0`` is
    False, so the restore falls through to ``inc(nan)`` and poisons the counter
    for the life of the process.
    """
    assert dj._coerce_total(raw) == 0.0, why


@pytest.mark.unit
def test_a_legitimate_positive_total_is_preserved() -> None:
    """The other half of the guard: real values still pass through unchanged."""
    assert dj._coerce_total(4) == 4.0
    assert dj._coerce_total(4.0) == 4.0


# ---------------------------------------------------------------------------
# readable=False means "a signal was lost" and is not the same as total == 0
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_absent_journal_is_a_readable_zero(tmp_path: Path) -> None:
    """First boot is the normal state and must not look like data loss.

    If an absent file reported ``readable=False`` every fresh deploy would emit
    the lifespan's "drain-timeout history may be under-reported" warning, which
    trains an operator to ignore it — in the one place it actually matters.
    """
    journal = dj.read_drain_journal(tmp_path / "nope.json")

    assert journal.total == 0.0
    assert journal.readable is True
    assert journal.last_inflight_count is None
    assert journal.last_timeout_seconds is None


@pytest.mark.unit
def test_journal_that_cannot_be_opened_is_an_unreadable_zero(tmp_path: Path) -> None:
    """An OSError on the read is a lost signal, not an empty deployment.

    A directory where the journal should be is the reproducible stand-in for
    the real cases — a permission change, an unmounted volume, a path pointed
    at the wrong thing. All raise ``OSError`` from ``read_text`` and all mean
    the same: whatever the previous process recorded, this one cannot see it.
    """
    path = tmp_path / "journal.json"
    path.mkdir()

    journal = dj.read_drain_journal(path)

    assert journal.total == 0.0
    assert journal.readable is False


@pytest.mark.unit
def test_corrupt_journal_is_an_unreadable_zero(tmp_path: Path) -> None:
    """COMPANION — a truncated or garbled file reports the loss, not a clean zero.

    Kept so the four-way table (absent / unopenable / corrupt / wrong-shape) is
    readable as a whole; this row is already covered by the existing suite.
    """
    path = tmp_path / "journal.json"
    path.write_text("{not json", encoding="utf-8")

    journal = dj.read_drain_journal(path)

    assert journal.total == 0.0
    assert journal.readable is False


@pytest.mark.unit
def test_non_object_journal_is_an_unreadable_zero_and_never_raises(
    tmp_path: Path,
) -> None:
    """Valid JSON of the wrong shape still may not raise.

    A bare list parses cleanly, so it gets past the ``JSONDecodeError`` branch
    and reaches the ``.get`` calls. The isinstance guard is what keeps the
    never-raises contract true — and this module is called during shutdown and
    at startup, where an exception costs the process either its drain record or
    its boot.
    """
    path = tmp_path / "journal.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    journal = dj.read_drain_journal(path)

    assert journal.total == 0.0
    assert journal.readable is False


@pytest.mark.unit
def test_non_numeric_side_fields_do_not_raise(tmp_path: Path) -> None:
    """A bad ``last_inflight_count`` degrades to None rather than raising.

    The total is the load-bearing field; the two side fields are diagnostics
    that ride along. Relaxing their type guards turns a diagnostic into a
    startup crash — ``int("many")`` raises straight out of a function whose
    entire contract is that it never does.
    """
    path = tmp_path / "journal.json"
    path.write_text(
        json.dumps(
            {
                "drain_timeout_total": 2.0,
                "last_inflight_count": "many",
                "last_timeout_seconds": "later",
            }
        ),
        encoding="utf-8",
    )

    journal = dj.read_drain_journal(path)

    assert journal.total == 2.0
    assert journal.readable is True
    assert journal.last_inflight_count is None
    assert journal.last_timeout_seconds is None


# ---------------------------------------------------------------------------
# Durability mechanics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_journal_is_fsynced_before_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsynced write is exactly the event this module exists to survive.

    The design premise is that the process observing the timeout is dying. A
    write still sitting in the page cache when the machine goes down loses the
    event as thoroughly as never writing it. Order matters as much as
    presence: the fsync has to land before the replace, or the replace can
    publish a filename whose contents are not yet on disk.
    """
    # ``dj.os`` IS the stdlib os module, so these wrappers observe every fsync
    # and replace in the process, not only this module's. Rather than assert
    # whole-process call equality — which any unrelated background write landing
    # inside the window would break, failing the test on something it does not
    # test — each call is recorded with the path it acted on and the assertion
    # is scoped to this test's own ``tmp_path``.
    #
    # Attributing the fsync needs the temp file's name, since fsync only sees a
    # descriptor. ``dj.tempfile`` is replaced with a one-attribute namespace
    # (not an attribute poked onto the shared tempfile module) so that patch, at
    # least, really is module-local, and it records the fd -> name mapping the
    # journal write is about to use.
    calls: list[tuple[str, str]] = []
    fd_names: dict[int, str] = {}
    real_fsync = os.fsync
    real_replace = os.replace
    real_named_temp_file = tempfile.NamedTemporaryFile

    def _named_temp_file(*args: Any, **kwargs: Any) -> Any:
        handle = real_named_temp_file(*args, **kwargs)
        fd_names[handle.fileno()] = handle.name
        return handle

    def _fsync(fd: int) -> None:
        calls.append(("fsync", fd_names.get(fd, f"<unknown fd {fd}>")))
        real_fsync(fd)

    def _replace(src: Any, dst: Any) -> None:
        calls.append(("replace", os.fspath(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(dj, "tempfile", SimpleNamespace(NamedTemporaryFile=_named_temp_file))
    monkeypatch.setattr(dj.os, "fsync", _fsync)
    monkeypatch.setattr(dj.os, "replace", _replace)

    dj.record_drain_timeout(
        inflight_count=1, timeout_seconds=1.0, path=tmp_path / "journal.json"
    )

    # The temp file is created in ``resolved.parent`` (same filesystem, so the
    # replace is atomic) and the destination is the journal itself, so both of
    # this module's calls land under tmp_path and nothing else does.
    ours = [op for op, target in calls if target.startswith(str(tmp_path))]
    assert ours == ["fsync", "replace"], (
        f"expected fsync-then-replace on this journal, got {calls!r}"
    )


@pytest.mark.unit
def test_a_failed_publish_leaves_no_temp_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write cleans up after itself.

    In the default configuration the journal lives beside the process's working
    directory, so a leaked ``.parallax_drain_journal.json.*.tmp`` accumulates
    one per failed shutdown — in the directory an operator is most likely to be
    staring at during the incident that caused them.
    """
    path = tmp_path / "journal.json"

    def _boom(src: Any, dst: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(dj.os, "replace", _boom)

    assert dj.record_drain_timeout(inflight_count=1, timeout_seconds=1.0, path=path) == 0.0
    assert list(tmp_path.iterdir()) == [], "a failed write must not leak its temp file"


@pytest.mark.unit
def test_an_unwritable_journal_fails_open_with_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing to persist may not raise: this runs during lifespan shutdown.

    An exception here surfaces as a lifespan failure and buries the drain
    timeout it was trying to record — losing the signal *and* misattributing
    the shutdown. The module docstring calls this posture out explicitly and
    nothing asserted it.
    """

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(dj.tempfile, "NamedTemporaryFile", _boom)

    assert (
        dj.record_drain_timeout(
            inflight_count=1, timeout_seconds=1.0, path=tmp_path / "journal.json"
        )
        == 0.0
    )


@pytest.mark.unit
def test_record_returns_the_new_running_total(tmp_path: Path) -> None:
    """The return value is the caller's only in-band confirmation.

    ``0.0`` is the documented "could not persist" answer, so returning it on
    success erases the distinction — and the accumulation is what makes a
    second timeout distinguishable from the first one being restored again.
    """
    path = tmp_path / "journal.json"

    assert dj.record_drain_timeout(inflight_count=1, timeout_seconds=1.0, path=path) == 1.0
    assert dj.record_drain_timeout(inflight_count=2, timeout_seconds=2.0, path=path) == 2.0
    assert dj.read_drain_journal(path).total == 2.0
