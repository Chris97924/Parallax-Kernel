"""Mutation-hardening for ``parallax.backup`` (land-20260824 w5 S5).

Additive companion to ``test_backup_restore.py`` / ``test_backup_cloud.py``.

TALLY — applied 33 / killed-by-new 21 / already-covered 11 / equivalent 1 /
unaddressed 0. Thirty-three semantic mutants were applied one at a time to a
pristine tree: 11 died against the pre-existing suite and 22 walked through it.
Of those 22, 21 are killed by the tests below and 1 (``B09``) is proven
EQUIVALENT — see ``TestDigest.test_the_read_window_cannot_change_the_digest``.

``backup.py`` is the thinnest-covered module in this lane: the existing suite
proves an end-to-end round-trip and two guard clauses, and almost nothing about
the MANIFEST the round-trip is supposed to make verifiable.

What the existing suite is blind to, and why
--------------------------------------------

  * **The manifest is only ever compared against ITSELF.**
    ``test_manifest_round_trip`` builds a ``BackupManifest`` by hand and
    asserts ``from_dict(to_dict(m)) == m``, which is true of almost any
    symmetric pair of functions. It never feeds ``from_dict`` a dict that came
    from JSON, so both integer coercions can be deleted and a manifest read
    back from disk silently carries strings where the dataclass promises
    ints — comparing unequal to the same manifest built in memory. And
    ``to_dict`` can stop defensively copying, so a caller mutating the dict it
    was handed corrupts the live manifest.

  * **Nothing reads the counts the manifest exists to carry.** No test asserts
    WHICH tables are counted, so ``index_state`` can drop out of the tuple;
    none creates two rows sharing a ``content_hash`` (legal across users), so
    the ``COUNT(DISTINCT content_hash)`` can lose its DISTINCT; and none makes
    the memory and claim counts differ, so the claims count can be read from
    the memories table.

  * **``_schema_version`` is never exercised off the happy path.** Every test
    runs a fully migrated DB, so ``MAX`` can become ``MIN`` (reporting the
    oldest migration as the schema version — a restore-compatibility check
    reading this would wave through a far newer archive) and the
    ``COALESCE(..., 0)`` default for a never-migrated DB can be anything.

  * **The digest is asserted to round-trip, not to be sha256.**
    ``test_restore_verify_detects_tampering`` proves the digest CHANGES when
    the bytes change, which is equally true of sha1 or md5. Nothing pins the
    algorithm or the length.

  * **The WAL checkpoint's three behaviours are all unobserved.** No test
    creates a backup from a database with a live WAL, so ``TRUNCATE`` can
    become ``PASSIVE`` (leaving the stale WAL that step 1 of the documented
    contract exists to eliminate), the "no row" guard can be deleted, and the
    ``!= 0`` result check can be relaxed to ``< 0`` — which accepts
    ``busy=1``, the exact code meaning "the checkpoint did NOT complete", and
    ships a torn database.

  * **Both entry guards are tested only with the file absent, never with the
    WRONG KIND of thing present.** ``is_file()`` can relax to ``exists()``
    (so a DIRECTORY at ``cfg.db_path`` gets past the check and dies later with
    an opaque sqlite error), and the vault's ``is_dir()`` likewise (so a plain
    FILE at ``cfg.vault_path`` is tarred in under the ``vault/`` prefix, where
    restore expects a tree).

  * **The cloud dispatch is only ever given canonical URIs.** Every cloud test
    passes a well-formed ``s3://bucket/key`` or an absolute local path, so
    ``startswith("s3://")`` can shrink to ``startswith("s3")`` on both the
    upload and download side — hijacking any ordinary relative path that
    happens to begin with those two letters — and ``_parse_s3_uri``'s scheme
    check and its empty-bucket/key check can both be relaxed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import sqlite3
import tarfile
from collections.abc import Callable

import pytest

import parallax.backup as _backup
from parallax.backup import (
    MANIFEST_NAME,
    BackupManifest,
    _checkpoint_truncate,
    _parse_s3_uri,
    _sha256_file,
    compute_manifest_from_db,
    create_backup,
    download_from,
    upload_to,
)
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect

_VERSION = "0.0.0-test"


@dataclasses.dataclass
class _Cfg:
    db_path: pathlib.Path
    vault_path: pathlib.Path


@pytest.fixture()
def cfg(tmp_path: pathlib.Path) -> _Cfg:
    db = tmp_path / "db" / "parallax.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    c = connect(db)
    migrate_to_latest(c)
    c.close()
    return _Cfg(db_path=db, vault_path=vault)


class _Sqlite3Proxy:
    """Stand-in for the ``sqlite3`` module inside ONE importer's namespace.

    Anything not explicitly overridden falls through to the real module, so a
    module that reaches for ``sqlite3.Connection`` or ``sqlite3.Error`` after
    the swap still sees the genuine objects.
    """

    def __init__(self, **overrides: object) -> None:
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        try:
            return self._overrides[name]
        except KeyError:
            return getattr(sqlite3, name)


def _patch_backup_connect(
    monkeypatch: pytest.MonkeyPatch, factory: Callable[..., object]
) -> None:
    """Route ``parallax.backup``'s ``sqlite3.connect`` to ``factory``.

    ``parallax.backup`` does a plain ``import sqlite3``, so its ``sqlite3``
    attribute IS the shared stdlib module object: setting
    ``parallax.backup.sqlite3.connect`` would replace ``sqlite3.connect`` for
    every importer in the process for the duration of the test. That is
    currently unobservable — pytest runs these serially, and xdist forks
    separate processes — but it becomes a real cross-test hazard under any
    in-process thread parallelism.

    Rebinding the ``sqlite3`` NAME in backup's own globals is the smallest
    scope that still intercepts the call, and ``monkeypatch`` restores the
    original binding at teardown whether the test passes or raises.
    """
    monkeypatch.setattr(_backup, "sqlite3", _Sqlite3Proxy(connect=factory))


def _seed_rows(db_path: pathlib.Path) -> None:
    """Two memories sharing one content_hash across users + two distinct claims."""
    c = sqlite3.connect(str(db_path))
    try:
        c.execute(
            "INSERT INTO sources(source_id, uri, kind, content_hash, user_id, "
            "ingested_at, state) VALUES ('s1','u','file','hs','u1','t','ingested')"
        )
        for i, user in enumerate(("u1", "u2")):
            c.execute(
                "INSERT INTO memories(memory_id, user_id, source_id, vault_path, "
                "title, summary, content_hash, state, created_at, updated_at) "
                "VALUES (?, ?, 's1', 'v.md', 't', 's', 'shared-hash', 'active', 't', 't')",
                (f"m{i}", user),
            )
        for i in range(2):
            c.execute(
                "INSERT INTO claims(claim_id, user_id, subject, predicate, object, "
                "source_id, content_hash, confidence, state, created_at, updated_at) "
                "VALUES (?, 'u1', 'a', 'b', 'c', 's1', ?, 0.5, 'auto', 't', 't')",
                (f"c{i}", f"claim-hash-{i}"),
            )
        c.commit()
    finally:
        c.close()


def _manifest_from_archive(archive: pathlib.Path) -> tuple[str, dict]:
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(MANIFEST_NAME)
        assert member is not None
        text = member.read().decode("utf-8")
    return text, json.loads(text)


# ---------------------------------------------------------------------------
# BackupManifest round-trip
# ---------------------------------------------------------------------------


class TestManifestRoundTrip:
    @staticmethod
    def _manifest() -> BackupManifest:
        return BackupManifest(
            parallax_version="0.2.1",
            schema_version=5,
            created_at="2026-04-18T00:00:00+00:00",
            db_sha256="a" * 64,
            row_counts={"memories": 1},
            content_hash_counts={"memories_hash_count": 1},
        )

    @pytest.mark.unit
    def test_to_dict_hands_out_copies_not_the_live_mappings(self) -> None:
        """Mutating the returned dict must not reach back into the manifest.

        ``test_manifest_round_trip`` only feeds ``to_dict``'s output straight
        back into ``from_dict``, so the two defensive ``dict(...)`` copies look
        redundant. They are the only thing stopping ``create_backup``'s
        json.dumps payload — or any caller that post-processes the dict —
        from editing the frozen manifest it is supposed to be describing.
        """
        m = self._manifest()

        d = m.to_dict()
        d["row_counts"]["memories"] = 999
        d["content_hash_counts"]["memories_hash_count"] = 999

        assert m.row_counts == {"memories": 1}
        assert m.content_hash_counts == {"memories_hash_count": 1}

    @pytest.mark.unit
    def test_from_dict_coerces_json_strings_back_to_ints(self) -> None:
        """The input is JSON off disk, where every number may arrive as a str.

        The existing round-trip test never crosses a real serialisation
        boundary that could change types, so both ``int(...)`` coercions can be
        deleted and a manifest restored from an archive written by an older (or
        sloppier) writer compares unequal to the identical manifest built in
        memory — silently failing every verification that compares manifests.
        """
        restored = BackupManifest.from_dict(
            {
                "parallax_version": "0.2.1",
                "schema_version": "5",
                "created_at": "2026-04-18T00:00:00+00:00",
                "db_sha256": "a" * 64,
                "row_counts": {"memories": "3"},
                "content_hash_counts": {"memories_hash_count": "2"},
            }
        )

        assert restored.schema_version == 5
        assert isinstance(restored.schema_version, int)
        assert restored.row_counts == {"memories": 3}
        assert restored.content_hash_counts == {"memories_hash_count": 2}


# ---------------------------------------------------------------------------
# The counts the manifest exists to carry
# ---------------------------------------------------------------------------


class TestManifestCounts:
    @pytest.mark.unit
    def test_all_six_canonical_tables_are_counted(self, cfg: _Cfg) -> None:
        """The counted-table tuple IS the manifest's coverage claim.

        Nothing asserts which tables appear in ``row_counts``, so a table can
        silently drop out and every restore verification that compares counts
        simply stops looking at it. Pinned as a literal set.
        """
        manifest = compute_manifest_from_db(cfg.db_path, parallax_version=_VERSION)

        assert set(manifest.row_counts) == {
            "sources",
            "memories",
            "claims",
            "decisions",
            "events",
            "index_state",
        }

    @pytest.mark.unit
    def test_the_hash_counts_are_distinct_and_read_their_own_tables(
        self, cfg: _Cfg
    ) -> None:
        """DISTINCT, and claims counted from ``claims``.

        The schema lets two users hold the same memory ``content_hash``
        (``UNIQUE(content_hash, user_id)``), which is exactly the case that
        separates ``COUNT(DISTINCT content_hash)`` from ``COUNT(content_hash)``
        — and no existing test creates it. The two counts are also seeded to
        DIFFER so the claims query cannot quietly read the memories table.
        """
        _seed_rows(cfg.db_path)

        manifest = compute_manifest_from_db(cfg.db_path, parallax_version=_VERSION)

        assert manifest.row_counts["memories"] == 2
        assert manifest.content_hash_counts["memories_hash_count"] == 1
        assert manifest.content_hash_counts["claims_hash_count"] == 2


class TestSchemaVersion:
    @staticmethod
    def _set_versions(db_path: pathlib.Path, versions: list[int]) -> None:
        c = sqlite3.connect(str(db_path))
        try:
            c.execute("DELETE FROM schema_migrations")
            for v in versions:
                c.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (v, f"m{v}", "2026-01-01T00:00:00+00:00"),
                )
            c.commit()
        finally:
            c.close()

    @pytest.mark.unit
    def test_the_schema_version_is_the_newest_applied_migration(
        self, cfg: _Cfg
    ) -> None:
        """MAX, not MIN.

        Every test runs a fully migrated DB where the ledger is contiguous, so
        nothing distinguishes the two aggregates. Reporting the OLDEST applied
        migration makes the manifest claim an ancient schema, and any
        restore-side compatibility gate reading it waves through an archive it
        cannot actually read.
        """
        self._set_versions(cfg.db_path, [1, 5, 9])

        manifest = compute_manifest_from_db(cfg.db_path, parallax_version=_VERSION)

        assert manifest.schema_version == 9

    @pytest.mark.unit
    def test_a_never_migrated_ledger_reports_version_zero(self, cfg: _Cfg) -> None:
        """``COALESCE(MAX(version), 0)`` — the empty-ledger default is 0.

        Zero is the documented "no migrations applied" sentinel; a negative
        default would be read as a valid (and impossibly old) schema version by
        any numeric comparison on the restore side.
        """
        self._set_versions(cfg.db_path, [])

        manifest = compute_manifest_from_db(cfg.db_path, parallax_version=_VERSION)

        assert manifest.schema_version == 0


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


class TestDigest:
    @pytest.mark.unit
    def test_the_digest_is_sha256_of_the_whole_file(
        self, cfg: _Cfg, tmp_path: pathlib.Path
    ) -> None:
        """Pinned to the ALGORITHM, not just to "it changes when bytes change".

        ``test_restore_verify_detects_tampering`` is satisfied by any hash
        function at all. The manifest field is named ``db_sha256`` and the
        restore path re-computes it, so the algorithm is a cross-version
        contract: an archive written with a different digest is unverifiable by
        every other build.
        """
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"x" * 3000)

        digest = _sha256_file(blob)

        assert len(digest) == 64
        assert digest == hashlib.sha256(b"x" * 3000).hexdigest()

    @pytest.mark.unit
    @pytest.mark.parametrize("size", [0, 1, 1023, 1024, 1025, 4096])
    def test_the_read_window_cannot_change_the_digest(
        self, tmp_path: pathlib.Path, size: int
    ) -> None:
        """EQUIVALENCE PROOF for the chunk-size mutant (``B09``).

        Changing ``fh.read(1024 * 1024)`` to ``fh.read(1024)`` survived the
        suite, and it survives this file too — because it is EQUIVALENT, not
        merely uncovered. ``hashlib`` digests are defined over the CONCATENATED
        byte stream fed to ``update()``: for any partition of the same bytes,
        the result is identical. The read window therefore chooses only I/O
        granularity, never the message, and no observable output can depend on
        it.

        This test pins the property that makes the equivalence hold — the
        digest equals sha256 of the whole file — across sizes straddling both
        candidate windows, rather than pretending to kill the mutant.
        """
        blob = tmp_path / f"blob-{size}.bin"
        payload = bytes(range(256)) * (size // 256) + b"\x00" * (size % 256)
        blob.write_bytes(payload)

        assert _sha256_file(blob) == hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# The WAL checkpoint (step 1 of the documented contract)
# ---------------------------------------------------------------------------


class TestWalCheckpoint:
    @pytest.mark.unit
    def test_the_checkpoint_truncates_the_wal_to_zero(
        self, cfg: _Cfg, tmp_path: pathlib.Path
    ) -> None:
        """TRUNCATE, not PASSIVE: the -wal file must come back empty.

        No existing test backs up a database with a live WAL, so the mode is
        unobserved. PASSIVE copies frames but leaves the WAL file at its old
        size, which is precisely the "stale WAL pages would make the copied
        main db file inconsistent" hazard the module docstring opens with.
        """
        keepalive = sqlite3.connect(str(cfg.db_path))
        try:
            keepalive.execute("PRAGMA journal_mode=WAL")
            keepalive.execute(
                "INSERT INTO sources(source_id, uri, kind, content_hash, user_id, "
                "ingested_at, state) VALUES ('s1','u','file','h','u1','t','ingested')"
            )
            keepalive.commit()
            wal = pathlib.Path(str(cfg.db_path) + "-wal")
            assert wal.exists() and wal.stat().st_size > 0

            create_backup(cfg, tmp_path / "backup.tar.gz")

            assert wal.stat().st_size == 0
        finally:
            keepalive.close()

    @pytest.mark.unit
    def test_a_checkpoint_that_returns_no_row_aborts(
        self, cfg: _Cfg, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``row is None`` guard has no test and is not decoration.

        Deleting it does not make the failure go away, it changes it from a
        named ``RuntimeError`` naming the pragma into a ``TypeError`` on
        ``None[0]`` two lines later — an unactionable traceback for the one
        failure mode that means "we could not make the db file consistent".
        """

        class _NoRowCursor:
            def fetchone(self) -> None:
                return None

        class _NoRowConn:
            def execute(self, *_a: object, **_k: object) -> _NoRowCursor:
                return _NoRowCursor()

            def close(self) -> None:
                return None

        _patch_backup_connect(monkeypatch, lambda *_a, **_k: _NoRowConn())

        with pytest.raises(RuntimeError, match="returned no row"):
            _checkpoint_truncate(cfg.db_path)

    @pytest.mark.unit
    @pytest.mark.parametrize("busy", [1, 2, -1])
    def test_any_non_zero_checkpoint_result_aborts(
        self, cfg: _Cfg, monkeypatch: pytest.MonkeyPatch, busy: int
    ) -> None:
        """``!= 0``: SQLite signals "checkpoint did not complete" with busy=1.

        Relaxing the check to ``< 0`` accepts exactly that value, so a
        checkpoint blocked by a concurrent reader is treated as success and the
        backup is taken from a database whose WAL was never folded in. Only 0
        means success, so the comparison must be an inequality against 0.
        """

        class _Cursor:
            def fetchone(self) -> tuple[int, int, int]:
                return (busy, 3, 3)

        class _Conn:
            def execute(self, *_a: object, **_k: object) -> _Cursor:
                return _Cursor()

            def close(self) -> None:
                return None

        _patch_backup_connect(monkeypatch, lambda *_a, **_k: _Conn())

        with pytest.raises(RuntimeError, match="wal_checkpoint"):
            _checkpoint_truncate(cfg.db_path)


# ---------------------------------------------------------------------------
# create_backup entry guards + archive shape
# ---------------------------------------------------------------------------


class TestCreateBackupGuards:
    @pytest.mark.unit
    def test_a_directory_at_the_db_path_is_rejected_as_missing(
        self, tmp_path: pathlib.Path
    ) -> None:
        """``is_file()``, not ``exists()``.

        ``test_backup_raises_if_db_missing`` deletes the path entirely, so the
        guard is never shown a path that EXISTS but is not a file. Relaxed to
        ``exists()``, a directory sails through and the failure surfaces much
        later as an opaque sqlite "unable to open database file" from inside
        the checkpoint.
        """
        cfg = _Cfg(db_path=tmp_path / "notadb", vault_path=tmp_path / "vault")
        cfg.db_path.mkdir()
        cfg.vault_path.mkdir()

        with pytest.raises(FileNotFoundError, match="does not exist"):
            create_backup(cfg, tmp_path / "backup.tar.gz")

    @pytest.mark.unit
    def test_a_plain_file_at_the_vault_path_is_not_archived(
        self, tmp_path: pathlib.Path
    ) -> None:
        """``is_dir()``, not ``exists()``: ``vault/`` is a TREE in the archive.

        Every existing test points ``vault_path`` at a real directory, so
        relaxing the check survives — and then a stray file at that path is
        tarred in under the ``vault`` arcname, where restore expects to walk a
        directory.
        """
        db = tmp_path / "db" / "parallax.db"
        db.parent.mkdir(parents=True)
        c = connect(db)
        migrate_to_latest(c)
        c.close()
        vault_file = tmp_path / "vault"
        vault_file.write_text("not a directory", encoding="utf-8")
        cfg = _Cfg(db_path=db, vault_path=vault_file)

        archive = tmp_path / "backup.tar.gz"
        create_backup(cfg, archive)

        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert "vault" not in names
        assert not any(n.startswith("vault") for n in names)
        assert "db/parallax.db" in names
        assert MANIFEST_NAME in names

    @pytest.mark.unit
    def test_the_manifest_json_is_written_with_sorted_keys(
        self, cfg: _Cfg, tmp_path: pathlib.Path
    ) -> None:
        """Deterministic serialisation is what makes two archives comparable.

        Nothing reads the manifest's raw bytes, so ``sort_keys=True`` can be
        dropped and the archive stops being reproducible for the same DB —
        defeating byte-comparison of two backups without changing a single
        parsed value.
        """
        archive = tmp_path / "backup.tar.gz"
        create_backup(cfg, archive)

        text, parsed = _manifest_from_archive(archive)
        keys = list(parsed.keys())

        assert keys == sorted(keys)
        assert text == json.dumps(parsed, sort_keys=True, indent=2)


# ---------------------------------------------------------------------------
# s3:// URI parsing
# ---------------------------------------------------------------------------


class TestParseS3Uri:
    @pytest.mark.unit
    @pytest.mark.parametrize("uri", ["s3:/bucket/key", "s3:bucket/key", "s3//bucket/key"])
    def test_only_the_full_scheme_is_accepted(self, uri: str) -> None:
        """``s3://`` — the slashes are part of the scheme test.

        ``test_bad_s3_uri_raises_value_error`` uses an obviously non-s3 string,
        so shortening the prefix to ``s3:`` survives — and a near-miss URI then
        gets past the guard and is sliced at a fixed offset, silently yielding
        a bucket name with the first characters chewed off.
        """
        with pytest.raises(ValueError, match="not an s3:// URI"):
            _parse_s3_uri(uri)

    @pytest.mark.unit
    @pytest.mark.parametrize("uri", ["s3://bucket/", "s3:///key"])
    def test_an_empty_bucket_or_key_is_rejected(self, uri: str) -> None:
        """``or``, not ``and``: EITHER half being empty is fatal.

        No test supplies a URI with one empty half, so the operator can be
        swapped and ``s3://bucket/`` parses to an empty key — which boto3
        happily turns into an upload to a zero-length object name.
        """
        with pytest.raises(ValueError, match="non-empty bucket and key"):
            _parse_s3_uri(uri)

    @pytest.mark.unit
    def test_a_well_formed_uri_still_splits_on_the_first_slash(self) -> None:
        """Control: keys keep their own slashes."""
        assert _parse_s3_uri("s3://b/a/b/c.tar.gz") == ("b", "a/b/c.tar.gz")


# ---------------------------------------------------------------------------
# upload_to / download_from dispatch
# ---------------------------------------------------------------------------


class TestCloudDispatch:
    @pytest.mark.unit
    def test_a_local_destination_beginning_with_s3_is_not_sent_to_boto3(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dispatch tests the SCHEME, not the first two letters.

        Every cloud test passes a canonical ``s3://...`` or an absolute local
        path, so ``startswith("s3://")`` can shrink to ``startswith("s3")``.
        Any ordinary relative path that happens to start with those letters —
        ``s3archive/nightly.tar.gz`` — is then hijacked into boto3.
        """
        monkeypatch.chdir(tmp_path)
        archive = tmp_path / "backup.tar.gz"
        archive.write_bytes(b"archive-bytes")

        upload_to(archive, "s3archive/nightly.tar.gz")

        written = tmp_path / "s3archive" / "nightly.tar.gz"
        assert written.is_file()
        assert written.read_bytes() == b"archive-bytes"

    @pytest.mark.unit
    def test_a_local_source_beginning_with_s3_is_not_fetched_from_boto3(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same hole on the download side."""
        monkeypatch.chdir(tmp_path)
        src = tmp_path / "s3source.tar.gz"
        src.write_bytes(b"source-bytes")

        download_from("s3source.tar.gz", tmp_path / "out.tar.gz")

        assert (tmp_path / "out.tar.gz").read_bytes() == b"source-bytes"

    @pytest.mark.unit
    def test_a_missing_archive_is_rejected_before_anything_is_created(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The pre-check must fire BEFORE the destination tree is made.

        ``test_upload_missing_archive_raises`` uses an s3 destination, where
        the guard is the only thing that can fail. On the LOCAL branch the
        error type is the same either way, so deleting the guard survives —
        but the local branch then runs ``dest.parent.mkdir(parents=True)``
        first, littering the filesystem with empty directories for an upload
        that was never going to happen, and reporting a bare errno instead of
        naming the archive.
        """
        dest_parent = tmp_path / "made" / "by" / "mistake"

        with pytest.raises(FileNotFoundError, match="archive not found"):
            upload_to(tmp_path / "missing.tar.gz", str(dest_parent / "out.tar.gz"))

        assert not dest_parent.exists()

    @pytest.mark.unit
    def test_download_creates_the_destination_tree(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The parent mkdir is load-bearing on the local branch too.

        Existing local-download coverage writes into a directory that already
        exists, so removing the mkdir survives there and fails for every caller
        that names a fresh destination directory.
        """
        src = tmp_path / "src.tar.gz"
        src.write_bytes(b"payload")
        dest = tmp_path / "brand" / "new" / "tree" / "out.tar.gz"

        download_from(str(src), dest)

        assert dest.read_bytes() == b"payload"

    @pytest.mark.unit
    def test_the_archive_member_layout_is_stable(
        self, cfg: _Cfg, tmp_path: pathlib.Path
    ) -> None:
        """Control on the three archive path constants restore reads back."""
        (cfg.vault_path / "note.md").write_text("hi", encoding="utf-8")
        archive = tmp_path / "backup.tar.gz"

        create_backup(cfg, archive)

        with tarfile.open(archive, "r:gz") as tar:
            names = {n.replace(os.sep, "/") for n in tar.getnames()}

        assert "db/parallax.db" in names
        assert "manifest.json" in names
        assert "vault" in names
