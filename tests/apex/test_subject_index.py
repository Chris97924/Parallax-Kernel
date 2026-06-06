"""Apex M7 Part B — subject index unit tests (#71 Gap 1, design doc D1).

Covers ``parallax.apex.subject_index`` in isolation: the data model, atomic
persistence, the M6 incremental writer, and — most importantly — the
``load_or_rebuild`` freshness contract (spec §8.4: rebuild-on-stale, never
silent-stale).

Freshness is tested without real packages: ``current_package_files`` only globs
by name, so empty ``*.aphelion.tar`` placeholder files drive the staleness logic
while a fake ``scan_fn`` stands in for the router's unpack scan.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from parallax.apex import subject_index
from parallax.apex.subject_index import (
    INDEX_FILENAME,
    IndexEntry,
    SubjectIndex,
    current_package_files,
    index_path,
    load_index,
    load_or_rebuild,
    save_index,
    update_index_for_package,
)


def _counter(metric: Any, **labels: str) -> float:
    child = metric.labels(**labels) if labels else metric
    return child._value.get()  # type: ignore[attr-defined]


def _touch_tar(package_dir: Path, name: str) -> None:
    (package_dir / name).write_bytes(b"")


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
            package_files=("x.aphelion.tar", "y.aphelion.tar"),
            built_at=1.0,
        )
        assert idx.subjects() == frozenset({"a", "b"})

    def test_packages_for_subject_returns_all_carrying_packages(self) -> None:
        idx = SubjectIndex(
            entries=(_entry("a", "y.aphelion.tar"), _entry("a", "x.aphelion.tar")),
            package_files=("x.aphelion.tar", "y.aphelion.tar"),
            built_at=1.0,
        )
        # Sorted, and every package carrying the subject is returned (R4 needs
        # the full claim set for a subject).
        assert idx.packages_for_subject("a") == ("x.aphelion.tar", "y.aphelion.tar")

    def test_packages_for_unknown_subject_is_empty(self) -> None:
        idx = SubjectIndex(entries=(), package_files=(), built_at=1.0)
        assert idx.packages_for_subject("nope") == ()

    def test_is_empty(self) -> None:
        assert SubjectIndex(entries=(), package_files=(), built_at=1.0).is_empty()
        assert not SubjectIndex(
            entries=(_entry("a", "x.aphelion.tar"),),
            package_files=("x.aphelion.tar",),
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
            package_files=("a.aphelion.tar",),
            built_at=123.5,
        )
        save_index(tmp_path, idx)
        loaded = load_index(tmp_path)
        assert loaded is not None
        assert loaded.entries == idx.entries
        assert loaded.package_files == idx.package_files
        assert loaded.built_at == idx.built_at

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_index(tmp_path) is None

    def test_load_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        index_path(tmp_path).write_text("{not valid json", encoding="utf-8")
        assert load_index(tmp_path) is None

    def test_load_schema_mismatch_returns_none(self, tmp_path: Path) -> None:
        index_path(tmp_path).write_text(
            '{"schema_version": 999, "built_at": 1.0, "package_files": [], "entries": []}',
            encoding="utf-8",
        )
        assert load_index(tmp_path) is None

    def test_save_leaves_no_temp_file(self, tmp_path: Path) -> None:
        save_index(tmp_path, SubjectIndex(entries=(), package_files=(), built_at=1.0))
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != INDEX_FILENAME]
        assert leftovers == []


# ===========================================================================
# M6 incremental writer
# ===========================================================================


@pytest.mark.unit
class TestUpdateIndexForPackage:
    def test_writes_subject_to_package_mapping(self, tmp_path: Path) -> None:
        _touch_tar(tmp_path, "pkg1.aphelion.tar")
        update_index_for_package(
            tmp_path,
            package_file="pkg1.aphelion.tar",
            package_id="pid-1",
            claim_subjects=[("claim-a", "retrieval-quality")],
        )
        idx = load_index(tmp_path)
        assert idx is not None
        assert idx.packages_for_subject("retrieval-quality") == ("pkg1.aphelion.tar",)
        assert idx.package_files == ("pkg1.aphelion.tar",)
        entry = idx.entries[0]
        assert (entry.package_id, entry.claim_id) == ("pid-1", "claim-a")

    def test_skips_subjectless_claims(self, tmp_path: Path) -> None:
        _touch_tar(tmp_path, "pkg1.aphelion.tar")
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
        _touch_tar(tmp_path, "pkg1.aphelion.tar")
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
        _touch_tar(tmp_path, "pkg1.aphelion.tar")
        update_index_for_package(
            tmp_path,
            package_file="pkg1.aphelion.tar",
            package_id="p1",
            claim_subjects=[("c1", "subject-1")],
        )
        # pkg2 added on disk; pkg1 removed from disk before pkg2 is indexed.
        _touch_tar(tmp_path, "pkg2.aphelion.tar")
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
        assert idx.package_files == ("pkg2.aphelion.tar",)


# ===========================================================================
# load_or_rebuild — the §8.4 freshness contract
# ===========================================================================


@pytest.mark.unit
class TestLoadOrRebuild:
    def test_missing_index_triggers_rebuild(self, tmp_path: Path) -> None:
        _touch_tar(tmp_path, "a.aphelion.tar")
        scanned = [_entry("subj", "a.aphelion.tar")]
        before = _counter(subject_index.INDEX_REBUILD, trigger="missing")

        idx = load_or_rebuild(tmp_path, lambda: scanned)

        assert idx.subjects() == frozenset({"subj"})
        assert idx.package_files == ("a.aphelion.tar",)
        assert _counter(subject_index.INDEX_REBUILD, trigger="missing") == before + 1
        # Rebuilt index was persisted for the next read.
        assert load_index(tmp_path) is not None

    def test_fresh_index_does_not_rescan(self, tmp_path: Path) -> None:
        _touch_tar(tmp_path, "a.aphelion.tar")
        save_index(
            tmp_path,
            SubjectIndex(
                entries=(_entry("subj", "a.aphelion.tar"),),
                package_files=("a.aphelion.tar",),
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
        _touch_tar(tmp_path, "a.aphelion.tar")
        save_index(
            tmp_path,
            SubjectIndex(
                entries=(_entry("a-subj", "a.aphelion.tar"),),
                package_files=("a.aphelion.tar",),
                built_at=1000.0,
            ),
        )
        _touch_tar(tmp_path, "b.aphelion.tar")  # new package, index doesn't know it
        rebuilt_entries = [_entry("a-subj", "a.aphelion.tar"), _entry("b-subj", "b.aphelion.tar")]
        before = _counter(subject_index.INDEX_REBUILD, trigger="stale")

        idx = load_or_rebuild(tmp_path, lambda: rebuilt_entries)

        assert idx.packages_for_subject("b-subj") == ("b.aphelion.tar",)  # not stale-empty
        assert idx.package_files == ("a.aphelion.tar", "b.aphelion.tar")
        assert _counter(subject_index.INDEX_REBUILD, trigger="stale") == before + 1
        assert subject_index.INDEX_STALENESS._value.get() > 0.0  # type: ignore[attr-defined]

    def test_corrupt_index_rebuilds(self, tmp_path: Path) -> None:
        _touch_tar(tmp_path, "a.aphelion.tar")
        index_path(tmp_path).write_text("{garbage", encoding="utf-8")
        before = _counter(subject_index.INDEX_REBUILD, trigger="corrupt")

        idx = load_or_rebuild(tmp_path, lambda: [_entry("subj", "a.aphelion.tar")])

        assert idx.subjects() == frozenset({"subj"})
        assert _counter(subject_index.INDEX_REBUILD, trigger="corrupt") == before + 1

    def test_rebuild_persist_failure_is_non_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed persist after rebuild still serves the in-memory index."""
        _touch_tar(tmp_path, "a.aphelion.tar")

        def _raise(*_a: Any, **_k: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(subject_index, "save_index", _raise)
        idx = load_or_rebuild(tmp_path, lambda: [_entry("subj", "a.aphelion.tar")])
        assert idx.subjects() == frozenset({"subj"})  # served despite persist failure

    def test_empty_corpus_rebuild_is_empty(self, tmp_path: Path) -> None:
        idx = load_or_rebuild(tmp_path, lambda: [])
        assert idx.is_empty()
        assert current_package_files(tmp_path) == ()
