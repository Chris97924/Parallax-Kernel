"""Mutation-hardening for ``parallax.apex.subject_index`` (overnight-20260816 S8).

Additive companion to ``test_subject_index.py``. Every test below was written
against a semantic mutant that the pre-existing suite let through.

The existing file covers the headline freshness rule well — a package added,
removed, or swapped under the same basename all rebuild. What it leaves unpinned
is everything *around* that decision:

  * **What counts as a package.** The freshness signature globs
    ``*.aphelion.tar`` specifically. Nothing observes that an unrelated tar
    dropped in the package dir does not invalidate the index and force every
    subsequent read onto the O(all packages) rebuild path.

  * **The observability half of spec §8.4.** Option (b) says staleness is
    alertable: the gauge must carry the real observed age on a stale read and
    return to ``0.0`` on the next fresh one, and the rebuild counter must say
    *why* it rebuilt. No existing case reads either metric, so a gauge pinned at
    zero — or one that latches high forever after a single stale read — is
    invisible.

  * **Cache-miss self-healing.** A rebuild must record the *current* identity
    set, otherwise every later read re-detects staleness and pays the full
    rebuild again. The existing rebuild cases each call ``load_or_rebuild``
    once, so the second call is never observed.

  * **"A derived cache must never break a read."** ``load_index`` swallows a
    corrupt file, but the existing corruption case is malformed JSON only —
    JSON that parses to a non-mapping, an unreadable path, and an index written
    by a *newer* schema all take different branches.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from parallax.apex.subject_index import (
    INDEX_REBUILD,
    INDEX_STALENESS,
    SCHEMA_VERSION,
    IndexEntry,
    PackageStat,
    SubjectIndex,
    current_packages,
    index_path,
    load_index,
    load_or_rebuild,
    save_index,
    update_index_for_package,
)


@pytest.fixture()
def package_dir(tmp_path: Path) -> Path:
    d = tmp_path / "packages"
    d.mkdir()
    return d


def _write_package(package_dir: Path, name: str, payload: bytes = b"tar-bytes") -> Path:
    path = package_dir / name
    path.write_bytes(payload)
    return path


def _entry(subject: str, package_file: str) -> IndexEntry:
    return IndexEntry(
        subject=subject,
        package_id=f"pid-{package_file}",
        claim_id=f"cid-{subject}",
        package_file=package_file,
    )


def _staleness() -> float:
    return INDEX_STALENESS._value.get()  # type: ignore[attr-defined]


def _rebuilds(trigger: str) -> float:
    return INDEX_REBUILD.labels(trigger=trigger)._value.get()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# What counts as a package (the freshness signature's glob)
# ---------------------------------------------------------------------------


def test_freshness_ignores_tars_that_are_not_aphelion_packages(
    package_dir: Path,
) -> None:
    """Only ``*.aphelion.tar`` participates in the identity signature.

    Widening the glob would let any unrelated archive dropped in the package
    dir invalidate the index, pushing every read onto the O(all packages)
    rebuild fallback the fast path exists to avoid.
    """
    _write_package(package_dir, "alpha.aphelion.tar")
    calls: list[int] = []

    def scan() -> list[IndexEntry]:
        calls.append(1)
        return [_entry("subject:alpha", "alpha.aphelion.tar")]

    load_or_rebuild(package_dir, scan)
    assert len(calls) == 1

    _write_package(package_dir, "operator-backup.tar", b"not a package")
    load_or_rebuild(package_dir, scan)

    assert len(calls) == 1, "an unrelated tar must not invalidate the index"


def test_a_package_vanishing_between_glob_and_stat_is_skipped(
    package_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent removal is a normal race, not a read failure.

    M6 ingest and a read can interleave; a file listed by the glob may be gone
    by the time it is stat'd. Propagating that would break the read instead of
    self-healing on the next one.
    """
    _write_package(package_dir, "alpha.aphelion.tar")
    _write_package(package_dir, "vanishing.aphelion.tar")

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "vanishing.aphelion.tar":
            raise FileNotFoundError(2, "removed between glob and stat")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert [pkg.name for pkg in current_packages(package_dir)] == ["alpha.aphelion.tar"]


# ---------------------------------------------------------------------------
# SubjectIndex model
# ---------------------------------------------------------------------------


def test_packages_for_subject_requires_an_exact_subject_match() -> None:
    """Subjects are opaque labels, not prefixes.

    A substring match would route a read for ``cat`` at every package holding
    ``cat food`` too, handing R4 a claim set for a *different* subject — and R4
    groups on subject, so supersession and contradiction would be computed
    across unrelated claims.
    """
    index = SubjectIndex(
        entries=(
            _entry("cat", "cat.aphelion.tar"),
            _entry("cat food", "catfood.aphelion.tar"),
        ),
        packages=(),
        built_at=0.0,
    )

    assert index.packages_for_subject("cat") == ("cat.aphelion.tar",)
    assert index.packages_for_subject("cat food") == ("catfood.aphelion.tar",)


def test_is_empty_tracks_backing_packages_not_entries() -> None:
    """"Empty" means *no corpus*, which is not the same as no routable subject.

    A package whose claims are all subjectless contributes zero entries while
    still being a real ingested package; reporting that corpus as empty would
    let a caller take the fresh-deploy path over a populated store.
    """
    fresh_deploy = SubjectIndex(entries=(), packages=(), built_at=0.0)
    assert fresh_deploy.is_empty() is True

    subjectless_corpus = SubjectIndex(
        entries=(),
        packages=(PackageStat(name="alpha.aphelion.tar", size=10, mtime_ns=1),),
        built_at=0.0,
    )
    assert subjectless_corpus.is_empty() is False


# ---------------------------------------------------------------------------
# load_index — a derived cache must never break a read
# ---------------------------------------------------------------------------


def test_index_written_by_a_newer_schema_is_treated_as_absent(
    package_dir: Path,
) -> None:
    """The schema gate is equality, not a floor.

    A rolled-back deploy meets an index written by the newer version; reading
    it under the old shape's assumptions is exactly the mis-parse the gate
    exists to prevent, so it must rebuild rather than trust the file.
    """
    payload = {
        "schema_version": SCHEMA_VERSION + 1,
        "built_at": 1.0,
        "packages": [{"name": "alpha.aphelion.tar", "size": 1, "mtime_ns": 2}],
        "entries": [
            {
                "subject": "s",
                "package_id": "p",
                "claim_id": "c",
                "package_file": "alpha.aphelion.tar",
            }
        ],
    }
    index_path(package_dir).write_text(json.dumps(payload), encoding="utf-8")

    assert load_index(package_dir) is None


def test_json_that_parses_to_a_non_mapping_is_treated_as_corrupt(
    package_dir: Path,
) -> None:
    """Valid JSON of the wrong *shape* is still corruption.

    A bare list parses cleanly, so the JSONDecodeError path never fires; without
    the mapping guard the subsequent lookup raises ``AttributeError``, which the
    corruption handler does not catch and which therefore escapes into the read.
    """
    index_path(package_dir).write_text("[]", encoding="utf-8")

    assert load_index(package_dir) is None


def test_an_unreadable_index_path_rebuilds_instead_of_raising(
    package_dir: Path,
) -> None:
    """Absent is not the only benign failure — unreadable is too.

    A directory (or a permissions-denied file) sitting at the index path raises
    an ``OSError`` that is not ``FileNotFoundError``; handling only the latter
    turns a derived-cache problem into a failed read.
    """
    index_path(package_dir).mkdir()

    assert load_index(package_dir) is None


# ---------------------------------------------------------------------------
# update_index_for_package — M6's best-effort write
# ---------------------------------------------------------------------------


def test_update_for_a_package_absent_from_disk_does_not_crash(
    package_dir: Path,
) -> None:
    """M6 may call in for a package no longer on disk (removed mid-ingest).

    It has no stat-able identity, so it must simply be left out of the
    signature — which also makes the index mismatch disk and forces the next
    read to rebuild. Recording a placeholder instead corrupts the signature.
    """
    _write_package(package_dir, "present.aphelion.tar")

    update_index_for_package(
        package_dir,
        package_file="ghost.aphelion.tar",
        package_id="pid-ghost",
        claim_subjects=[("cid-1", "subject:ghost")],
    )

    index = load_index(package_dir)
    assert index is not None
    assert index.package_files() == ()
    assert index.packages != current_packages(package_dir), "must not read as fresh"


# ---------------------------------------------------------------------------
# load_or_rebuild — spec §8.4 option (b) observability
# ---------------------------------------------------------------------------


def test_staleness_gauge_reports_the_observed_age_then_resets_when_fresh(
    package_dir: Path,
) -> None:
    """The gauge is the alertable signal that M6 is not maintaining the index.

    It must carry the real age on a stale read (a constant 0.0 makes a
    persistently stale index unalertable) and must return to 0.0 on the next
    fresh read (otherwise a single stale read latches the alert on forever).
    """
    _write_package(package_dir, "alpha.aphelion.tar")
    save_index(
        package_dir,
        SubjectIndex(
            entries=(),
            packages=(PackageStat(name="gone.aphelion.tar", size=1, mtime_ns=2),),
            built_at=time.time() - 600.0,
        ),
    )

    def scan() -> list[IndexEntry]:
        return [_entry("subject:alpha", "alpha.aphelion.tar")]

    load_or_rebuild(package_dir, scan)
    assert _staleness() >= 500.0, "a 10-minute-old index must report its age"

    load_or_rebuild(package_dir, scan)
    assert _staleness() == 0.0, "the gauge must clear once the index is fresh again"


def test_rebuild_counter_distinguishes_a_corrupt_index_from_a_missing_one(
    package_dir: Path,
) -> None:
    """``trigger`` is the label that tells an operator which fault they have.

    A missing index is a normal cold start; a corrupt one means something is
    writing garbage into the package dir. Collapsing them hides the second
    behind the first.
    """
    _write_package(package_dir, "alpha.aphelion.tar")

    def scan() -> list[IndexEntry]:
        return [_entry("subject:alpha", "alpha.aphelion.tar")]

    before_missing = _rebuilds("missing")
    before_corrupt = _rebuilds("corrupt")

    load_or_rebuild(package_dir, scan)
    assert _rebuilds("missing") == before_missing + 1
    assert _rebuilds("corrupt") == before_corrupt

    index_path(package_dir).write_text("{not json", encoding="utf-8")
    load_or_rebuild(package_dir, scan)

    assert _rebuilds("corrupt") == before_corrupt + 1
    assert _rebuilds("missing") == before_missing + 1


def test_a_rebuild_records_the_current_identity_so_the_next_read_is_fresh(
    package_dir: Path,
) -> None:
    """The rebuild is self-healing: it must leave the cache *fresh*.

    Persisting the identity set that was already known to be stale makes every
    subsequent read re-detect staleness and pay the O(all packages) scan again
    — the fast path would never engage after the first corpus change.
    """
    _write_package(package_dir, "alpha.aphelion.tar")
    calls: list[int] = []

    def scan() -> list[IndexEntry]:
        calls.append(1)
        return [_entry("subject:alpha", "alpha.aphelion.tar")]

    load_or_rebuild(package_dir, scan)  # cold start: index missing
    assert len(calls) == 1

    _write_package(package_dir, "beta.aphelion.tar")
    load_or_rebuild(package_dir, scan)  # corpus grew: stale rebuild
    assert len(calls) == 2

    load_or_rebuild(package_dir, scan)
    assert len(calls) == 2, "the rebuilt index must read as fresh"
