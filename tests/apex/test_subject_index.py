"""Apex M7 Part B — subject index unit tests (#71 Gap 1, design doc D1).

Covers ``parallax.apex.subject_index`` in isolation: the data model, atomic
persistence, the M6 incremental writer, and — most importantly — the
``load_or_rebuild`` freshness contract (spec §8.4: rebuild-on-stale, never
silent-stale), including the same-name content-swap case.

Freshness is tested without real packages: ``current_packages`` reads only
``os.stat`` metadata, so placeholder ``*.aphelion.tar`` files (whose bytes /
mtime drive the identity signal) plus a fake ``scan_fn`` standing in for the
router's unpack scan are enough.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from parallax.apex import subject_index
from parallax.apex.subject_index import (
    INDEX_FILENAME,
    IndexEntry,
    PackageStat,
    SubjectIndex,
    current_package_files,
    current_packages,
    index_path,
    load_index,
    load_or_rebuild,
    save_index,
    update_index_for_package,
)


def _counter(metric: Any, **labels: str) -> float:
    child = metric.labels(**labels) if labels else metric
    return child._value.get()  # type: ignore[attr-defined]


def _write_tar(package_dir: Path, name: str, content: bytes = b"") -> None:
    (package_dir / name).write_bytes(content)


def _ps(name: str, size: int = 0, mtime_ns: int = 0) -> PackageStat:
    return PackageStat(name=name, size=size, mtime_ns=mtime_ns)


def _entry(
    subject: str, package_file: str, claim_id: str = "c1", package_id: str = "p1"
) -> IndexEntry:
    return IndexEntry(
        subject=subject, package_id=package_id, claim_id=claim_id, package_file=package_file
    )


# ===========================================================================
# Data model
# ===========================================================================


@pytest.mark.unit
class TestSubjectIndexModel:
    def test_subjects_distinct(self) -> None:
        idx = SubjectIndex(
            entries=(
                _entry("a", "x.aphelion.tar"),
                _entry("a", "y.aphelion.tar"),
                _entry("b", "x.aphelion.tar"),
            ),
            packages=(_ps("x.aphelion.tar"), _ps("y.aphelion.tar")),
            built_at=1.0,
        )
        assert idx.subjects() == frozenset({"a", "b"})

    def test_packages_for_subject_returns_all_carrying_packages(self) -> None:
        idx = SubjectIndex(
            entries=(_entry("a", "y.aphelion.tar"), _entry("a", "x.aphelion.tar")),
            packages=(_ps("x.aphelion.tar"), _ps("y.aphelion.tar")),
            built_at=1.0,
        )
        # Sorted, and every package carrying the subject is returned (R4 needs
        # the full claim set for a subject).
        assert idx.packages_for_subject("a") == ("x.aphelion.tar", "y.aphelion.tar")

    def test_packages_for_unknown_subject_is_empty(self) -> None:
        idx = SubjectIndex(entries=(), packages=(), built_at=1.0)
        assert idx.packages_for_subject("nope") == ()

    def test_package_files_lists_names(self) -> None:
        idx = SubjectIndex(
            entries=(),
            packages=(_ps("b.aphelion.tar", 1, 2), _ps("a.aphelion.tar", 3, 4)),
            built_at=1.0,
        )
        assert idx.package_files() == ("b.aphelion.tar", "a.aphelion.tar")

    def test_is_empty(self) -> None:
        assert SubjectIndex(entries=(), packages=(), built_at=1.0).is_empty()
        assert not SubjectIndex(
            entries=(_entry("a", "x.aphelion.tar"),),
            packages=(_ps("x.aphelion.tar"),),
            built_at=1.0,
        ).is_empty()


# ===========================================================================
# Persistence
# ===========================================================================


@pytest.mark.unit
class TestPersistence:
    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        idx = SubjectIndex(
            entries=(
                _entry("retrieval-quality", "a.aphelion.tar", claim_id="cid", package_id="pid"),
            ),
            packages=(_ps("a.aphelion.tar", size=42, mtime_ns=123456789),),
            built_at=123.5,
        )
        save_index(tmp_path, idx)
        loaded = load_index(tmp_path)
        assert loaded is not None
        assert loaded.entries == idx.entries
        assert loaded.packages == idx.packages
        assert loaded.built_at == idx.built_at

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_index(tmp_path) is None

    def test_load_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        index_path(tmp_path).write_text("{not valid json", encoding="utf-8")
        assert load_index(tmp_path) is None

    def test_load_schema_mismatch_returns_none(self, tmp_path: Path) -> None:
        index_path(tmp_path).write_text(
            '{"schema_version": 1, "built_at": 1.0, "package_files": [], "entries": []}',
            encoding="utf-8",
        )
        assert load_index(tmp_path) is None

    def test_save_leaves_no_temp_file(self, tmp_path: Path) -> None:
        save_index(tmp_path, SubjectIndex(entries=(), packages=(), built_at=1.0))
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != INDEX_FILENAME]
        assert leftovers == []


# ===========================================================================
# M6 incremental writer
# ===========================================================================


@pytest.mark.unit
class TestUpdateIndexForPackage:
    def test_writes_subject_to_package_mapping(self, tmp_path: Path) -> None:
        _write_tar(tmp_path, "pkg1.aphelion.tar", b"content")
        update_index_for_package(
            tmp_path,
            package_file="pkg1.aphelion.tar",
            package_id="pid-1",
            claim_subjects=[("claim-a", "retrieval-quality")],
        )
        idx = load_index(tmp_path)
        assert idx is not None
        assert idx.packages_for_subject("retrieval-quality") == ("pkg1.aphelion.tar",)
        assert idx.package_files() == ("pkg1.aphelion.tar",)
        # Identity captured for staleness detection (non-zero size recorded).
        assert idx.packages[0].size == len(b"content")
        entry = idx.entries[0]
        assert (entry.package_id, entry.claim_id) == ("pid-1", "claim-a")

    def test_skips_subjectless_claims(self, tmp_path: Path) -> None:
        _write_tar(tmp_path, "pkg1.aphelion.tar")
        update_index_for_package(
            tmp_path,
            package_file="pkg1.aphelion.tar",
            package_id="pid-1",
            claim_subjects=[("claim-a", ""), ("claim-b", "real-subject")],
        )
        idx = load_index(tmp_path)
        assert idx is not None
        assert idx.subjects() == frozenset({"real-subject"})

    def test_reingest_replaces_package_entries(self, tmp_path: Path) -> None:
        _write_tar(tmp_path, "pkg1.aphelion.tar")
        update_index_for_package(
            tmp_path,
            package_file="pkg1.aphelion.tar",
            package_id="pid-1",
            claim_subjects=[("c1", "old-subject")],
        )
        update_index_for_package(
            tmp_path,
            package_file="pkg1.aphelion.tar",
            package_id="pid-1",
            claim_subjects=[("c1", "new-subject")],
        )
        idx = load_index(tmp_path)
        assert idx is not None
        assert idx.subjects() == frozenset({"new-subject"})  # old entry replaced

    def test_keeps_other_packages_prunes_vanished(self, tmp_path: Path) -> None:
        _write_tar(tmp_path, "pkg1.aphelion.tar")
        update_index_for_package(
            tmp_path,
            package_file="pkg1.aphelion.tar",
            package_id="p1",
            claim_subjects=[("c1", "subject-1")],
        )
        # pkg2 added on disk; pkg1 removed from disk before pkg2 is indexed.
        _write_tar(tmp_path, "pkg2.aphelion.tar")
        (tmp_path / "pkg1.aphelion.tar").unlink()
        update_index_for_package(
            tmp_path,
            package_file="pkg2.aphelion.tar",
            package_id="p2",
            claim_subjects=[("c2", "subject-2")],
        )
        idx = load_index(tmp_path)
        assert idx is not None
        # subject-1's package vanished → pruned; only on-disk pkg2 remains.
        assert idx.subjects() == frozenset({"subject-2"})
        assert idx.package_files() == ("pkg2.aphelion.tar",)

    def test_does_not_certify_changed_other_package_as_fresh(self, tmp_path: Path) -> None:
        """Codex #74 round 2: ingesting B must not stamp a changed A as fresh.

        A is same-name swapped on disk but its best-effort index write was
        skipped; ingesting B carries A's *recorded* (old) identity, not A's
        current one — so a later read still detects A as stale and rebuilds
        rather than serving A's stale subjects indefinitely (no silent-stale).
        """
        _write_tar(tmp_path, "a.aphelion.tar", b"a-v1")
        update_index_for_package(
            tmp_path,
            package_file="a.aphelion.tar",
            package_id="pa",
            claim_subjects=[("ca", "a-subject")],
        )
        # A's content is swapped on disk; its index write is skipped (we do NOT
        # call update_index_for_package for A).
        _write_tar(tmp_path, "a.aphelion.tar", b"a-v2-much-bigger-content")
        # Ingest B before any read rebuild.
        _write_tar(tmp_path, "b.aphelion.tar", b"b-v1")
        update_index_for_package(
            tmp_path,
            package_file="b.aphelion.tar",
            package_id="pb",
            claim_subjects=[("cb", "b-subject")],
        )

        idx = load_index(tmp_path)
        assert idx is not None
        a_stat = next(p for p in idx.packages if p.name == "a.aphelion.tar")
        # A's OLD identity is preserved (size of a-v1), NOT re-stamped to a-v2.
        assert a_stat.size == len(b"a-v1")

        # So a read detects A as stale and rebuilds (the silent-stale is gone).
        before = _counter(subject_index.INDEX_REBUILD, trigger="stale")
        rebuilt = load_or_rebuild(
            tmp_path,
            lambda: [
                _entry("a-subject-new", "a.aphelion.tar"),
                _entry("b-subject", "b.aphelion.tar"),
            ],
        )
        assert _counter(subject_index.INDEX_REBUILD, trigger="stale") == before + 1
        assert rebuilt.subjects() == frozenset({"a-subject-new", "b-subject"})

    def test_update_keeps_packages_sorted_so_next_read_is_fresh(self, tmp_path: Path) -> None:
        """Codex #74 round 3: a clean M6 update must leave the index fresh.

        Persisted package identities are name-sorted to match current_packages(),
        so load_or_rebuild() sees an exact match and takes the fast path instead
        of a spurious full rebuild. Names are chosen so a naive append would
        mis-order them (ingest 'a' while 'b' is already indexed).
        """
        _write_tar(tmp_path, "b.aphelion.tar")
        update_index_for_package(
            tmp_path,
            package_file="b.aphelion.tar",
            package_id="pb",
            claim_subjects=[("cb", "b-subj")],
        )
        _write_tar(tmp_path, "a.aphelion.tar")  # sorts BEFORE b
        update_index_for_package(
            tmp_path,
            package_file="a.aphelion.tar",
            package_id="pa",
            claim_subjects=[("ca", "a-subj")],
        )

        idx = load_index(tmp_path)
        assert idx is not None
        assert idx.package_files() == ("a.aphelion.tar", "b.aphelion.tar")  # sorted

        def _no_scan() -> list[IndexEntry]:
            raise AssertionError("a clean M6 update must not trigger a rebuild")

        before = _counter(subject_index.INDEX_REBUILD, trigger="stale")
        result = load_or_rebuild(tmp_path, _no_scan)
        assert _counter(subject_index.INDEX_REBUILD, trigger="stale") == before  # no rebuild
        assert result.subjects() == frozenset({"a-subj", "b-subj"})


# ===========================================================================
# load_or_rebuild — the §8.4 freshness contract
# ===========================================================================


@pytest.mark.unit
class TestLoadOrRebuild:
    def test_missing_index_triggers_rebuild(self, tmp_path: Path) -> None:
        _write_tar(tmp_path, "a.aphelion.tar")
        scanned = [_entry("subj", "a.aphelion.tar")]
        before = _counter(subject_index.INDEX_REBUILD, trigger="missing")

        idx = load_or_rebuild(tmp_path, lambda: scanned)

        assert idx.subjects() == frozenset({"subj"})
        assert idx.package_files() == ("a.aphelion.tar",)
        assert _counter(subject_index.INDEX_REBUILD, trigger="missing") == before + 1
        # Rebuilt index was persisted for the next read.
        assert load_index(tmp_path) is not None

    def test_fresh_index_does_not_rescan(self, tmp_path: Path) -> None:
        _write_tar(tmp_path, "a.aphelion.tar")
        save_index(
            tmp_path,
            SubjectIndex(
                entries=(_entry("subj", "a.aphelion.tar"),),
                packages=current_packages(tmp_path),
                built_at=10.0,
            ),
        )

        def _boom() -> list[IndexEntry]:
            raise AssertionError("scan_fn must NOT run when the index is fresh")

        idx = load_or_rebuild(tmp_path, _boom)
        assert idx.subjects() == frozenset({"subj"})
        assert subject_index.INDEX_STALENESS._value.get() == 0.0  # type: ignore[attr-defined]

    def test_stale_index_rebuilds_no_silent_stale(self, tmp_path: Path) -> None:
        """A package on disk but absent from the index is NEVER served stale-empty.

        This is the spec §8.4 silent-stale prohibition: ``b`` landed after the
        index was built, so the index is stale; the read rebuilds synchronously
        and surfaces ``b`` rather than returning empty for it.
        """
        _write_tar(tmp_path, "a.aphelion.tar")
        save_index(
            tmp_path,
            SubjectIndex(
                entries=(_entry("a-subj", "a.aphelion.tar"),),
                packages=current_packages(tmp_path),
                built_at=1000.0,
            ),
        )
        _write_tar(tmp_path, "b.aphelion.tar")  # new package, index doesn't know it
        rebuilt_entries = [_entry("a-subj", "a.aphelion.tar"), _entry("b-subj", "b.aphelion.tar")]
        before = _counter(subject_index.INDEX_REBUILD, trigger="stale")

        idx = load_or_rebuild(tmp_path, lambda: rebuilt_entries)

        assert idx.packages_for_subject("b-subj") == ("b.aphelion.tar",)  # not stale-empty
        assert idx.package_files() == ("a.aphelion.tar", "b.aphelion.tar")
        assert _counter(subject_index.INDEX_REBUILD, trigger="stale") == before + 1
        assert subject_index.INDEX_STALENESS._value.get() > 0.0  # type: ignore[attr-defined]

    def test_same_name_content_swap_rebuilds(self, tmp_path: Path) -> None:
        """Codex #74 P1: a same-basename content change must trip a rebuild.

        If a package is replaced/re-ingested under the same ``.aphelion.tar``
        name and the best-effort M6 index write was skipped, a filename-only
        freshness check would keep serving the OLD subjects (silent-stale). The
        identity signal (size/mtime) catches it: here the file's bytes change
        (and mtime is bumped), so the read rebuilds and surfaces the new subject.
        """
        _write_tar(tmp_path, "a.aphelion.tar", b"v1")
        save_index(
            tmp_path,
            SubjectIndex(
                entries=(_entry("old-subj", "a.aphelion.tar"),),
                packages=current_packages(tmp_path),
                built_at=1000.0,
            ),
        )
        # Replace content under the SAME name (size changes) + force a new mtime
        # so the identity differs even on coarse-resolution filesystems.
        _write_tar(tmp_path, "a.aphelion.tar", b"v2-different-content")
        st = (tmp_path / "a.aphelion.tar").stat()
        os.utime(tmp_path / "a.aphelion.tar", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        before = _counter(subject_index.INDEX_REBUILD, trigger="stale")

        idx = load_or_rebuild(tmp_path, lambda: [_entry("new-subj", "a.aphelion.tar")])

        assert idx.subjects() == frozenset({"new-subj"})  # not the stale "old-subj"
        assert _counter(subject_index.INDEX_REBUILD, trigger="stale") == before + 1

    def test_mtime_only_change_rebuilds(self, tmp_path: Path) -> None:
        """Even a same-size content swap is caught via mtime (identity ≠ name)."""
        _write_tar(tmp_path, "a.aphelion.tar", b"same-size!!")
        save_index(
            tmp_path,
            SubjectIndex(
                entries=(_entry("old-subj", "a.aphelion.tar"),),
                packages=current_packages(tmp_path),
                built_at=1000.0,
            ),
        )
        st = (tmp_path / "a.aphelion.tar").stat()
        os.utime(tmp_path / "a.aphelion.tar", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        before = _counter(subject_index.INDEX_REBUILD, trigger="stale")

        idx = load_or_rebuild(tmp_path, lambda: [_entry("new-subj", "a.aphelion.tar")])

        assert idx.subjects() == frozenset({"new-subj"})
        assert _counter(subject_index.INDEX_REBUILD, trigger="stale") == before + 1

    def test_corrupt_index_rebuilds(self, tmp_path: Path) -> None:
        _write_tar(tmp_path, "a.aphelion.tar")
        index_path(tmp_path).write_text("{garbage", encoding="utf-8")
        before = _counter(subject_index.INDEX_REBUILD, trigger="corrupt")

        idx = load_or_rebuild(tmp_path, lambda: [_entry("subj", "a.aphelion.tar")])

        assert idx.subjects() == frozenset({"subj"})
        assert _counter(subject_index.INDEX_REBUILD, trigger="corrupt") == before + 1

    def test_rebuild_persist_failure_is_non_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed persist after rebuild still serves the in-memory index."""
        _write_tar(tmp_path, "a.aphelion.tar")

        def _raise(*_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(subject_index, "save_index", _raise)
        idx = load_or_rebuild(tmp_path, lambda: [_entry("subj", "a.aphelion.tar")])
        assert idx.subjects() == frozenset({"subj"})  # served despite persist failure

    def test_empty_corpus_rebuild_is_empty(self, tmp_path: Path) -> None:
        idx = load_or_rebuild(tmp_path, lambda: [])
        assert idx.is_empty()
        assert current_package_files(tmp_path) == ()
