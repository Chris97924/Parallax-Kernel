"""Apex M7 Part B — subject → package index (#71 Gap 1, design doc D1).

A persisted ``subject → {package_id, claim_id, package_file}`` index that the
free-text read path uses to resolve a candidate subject to the **specific**
``.aphelion.tar`` package(s) that carry it — instead of unpacking the entire
``PARALLAX_APHELION_PACKAGE_DIR`` on every query (spec §8.4 Q4 per-read
full-corpus-unpack ceiling).

Ownership / freshness (spec §8.4 stale-index window rule — silent-stale is
**forbidden**):

  * **M6 ingest writes it.** ``update_index_for_package`` is called by
    ``parallax.apex.aphelion_ingest.ingest_package`` after the audit rows
    commit, so steady-state reads hit the fast path (one JSON read + a set
    lookup, no full-corpus unpack).
  * **The read path guarantees correctness**, not M6. ``load_or_rebuild``
    treats the index as a *derived cache*: if the recorded package set does
    not match what is actually on disk it **rebuilds synchronously before
    returning** (spec §8.4 option (a)) and publishes
    ``parallax_apex_index_staleness_seconds`` (option (b)).

Freshness is keyed on package **identity** — ``(name, size, mtime_ns)`` — not
on the filename alone. A package replaced or re-ingested under the *same*
``.aphelion.tar`` basename (e.g. when the best-effort M6 index write was
skipped or failed) therefore still trips a rebuild: its size/mtime differ, so
a read never resolves against the stale subjects of the old content. The
identity is read from ``os.stat`` only (no content read), so the fast-path
latency budget (§4.2) is preserved.

Package-count ceiling (spec §8.4, load-bearing): the *fast path* cost is
O(matching packages) — it scales well past the ~100-package per-read ceiling
the spec warns about. The *rebuild fallback* is O(all packages) but only fires
on a cache miss (content changed / package added / removed), so it is the rare
self-healing path, not the steady state.

This module performs **no** package unpacking itself (that keeps it free of an
``aphelion`` dependency and of the router import, avoiding a cycle). The
read-path rebuild scan is injected as ``scan_fn`` by the router, which already
owns the safe unpack + verify surface.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import prometheus_client

__all__ = [
    "INDEX_FILENAME",
    "SCHEMA_VERSION",
    "IndexEntry",
    "PackageStat",
    "SubjectIndex",
    "current_package_files",
    "current_packages",
    "index_path",
    "load_index",
    "load_or_rebuild",
    "save_index",
    "update_index_for_package",
]

_log = logging.getLogger(__name__)

# Dot-prefixed so it never matches the ``*.aphelion.tar`` glob the read path
# and the freshness check rely on.
INDEX_FILENAME = ".apex-subject-index.json"

# Bumped when the on-disk JSON shape changes; an index written by an older
# schema is treated as absent (→ rebuild) rather than mis-parsed. v2 added
# per-package identity (size/mtime) to the freshness signal.
SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Observability (spec §8.4 option (b) — staleness is alertable even though the
# rebuild path (option (a)) is what actually preserves correctness). Idempotent
# registration mirrors the convention in ``parallax.apex.router`` /
# ``parallax.router.inflight`` so a test reload does not trip prometheus's
# duplicate-name ValueError.
# ---------------------------------------------------------------------------


def _get_or_create(factory: Callable[[], Any], base_name: str) -> Any:
    try:
        return factory()
    except ValueError:
        registry = prometheus_client.REGISTRY._names_to_collectors  # type: ignore[attr-defined]
        for collector in set(registry.values()):
            if getattr(collector, "_name", None) == base_name:
                return collector
        raise


INDEX_STALENESS = _get_or_create(
    lambda: prometheus_client.Gauge(
        "parallax_apex_index_staleness_seconds",
        "Seconds the subject index was out of date when a free-text read "
        "observed it (0.0 when fresh). A persistently non-zero value means M6 "
        "ingest is not maintaining the index and reads are paying the rebuild "
        "cost.",
    ),
    "parallax_apex_index_staleness_seconds",
)

INDEX_REBUILD = _get_or_create(
    lambda: prometheus_client.Counter(
        "parallax_apex_index_rebuild",
        "Subject-index synchronous rebuilds on the read path, by trigger.",
        ["trigger"],
    ),
    "parallax_apex_index_rebuild",
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    """One ``(subject, claim)`` location within a package.

    ``package_file`` is the on-disk ``.aphelion.tar`` basename — the routing
    key the read path needs to unpack exactly the right package(s).
    ``package_id`` (manifest id) and ``claim_id`` are the spec's
    ``{package_id, claim_key}`` payload (kept for audit/debug; the manifest id
    and the tar filename are not guaranteed equal, so both are stored).
    """

    subject: str
    package_id: str
    claim_id: str
    package_file: str


@dataclass(frozen=True)
class PackageStat:
    """Identity signature of a package file used for staleness detection.

    ``(name, size, mtime_ns)`` together change whenever a package's content is
    replaced — even under the same basename — so the read path can detect a
    same-name content swap that a filename-only check would miss. Read from
    ``os.stat`` (metadata only, no content read).
    """

    name: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SubjectIndex:
    """An immutable snapshot of the subject → package mapping.

    ``packages`` is the identity set of the ``.aphelion.tar`` files the index
    was built from; the read path compares it to the live filesystem to detect
    staleness (name, size, *and* mtime). ``built_at`` is an epoch-seconds stamp
    used to report staleness.
    """

    entries: tuple[IndexEntry, ...]
    packages: tuple[PackageStat, ...]
    built_at: float

    def subjects(self) -> frozenset[str]:
        """Distinct subject labels present in the index (fed to the resolver)."""
        return frozenset(entry.subject for entry in self.entries)

    def packages_for_subject(self, subject: str) -> tuple[str, ...]:
        """Sorted ``.aphelion.tar`` basenames that carry ``subject``.

        Returns every package holding the subject so the per-subject R4 read
        sees the full claim set (supersession/contradiction across packages is
        only correct when R4 is given all of a subject's claims).
        """
        files = {entry.package_file for entry in self.entries if entry.subject == subject}
        return tuple(sorted(files))

    def package_files(self) -> tuple[str, ...]:
        """Sorted basenames of the packages this index was built from."""
        return tuple(pkg.name for pkg in self.packages)

    def is_empty(self) -> bool:
        """True when no packages back this index (a fresh-deploy empty corpus)."""
        return len(self.packages) == 0


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def index_path(package_dir: Path | str) -> Path:
    return Path(package_dir) / INDEX_FILENAME


def current_packages(package_dir: Path | str) -> tuple[PackageStat, ...]:
    """Identity (name, size, mtime_ns) of every ``.aphelion.tar`` on disk.

    Sorted by name for a stable, comparable signature. A file that vanishes
    between glob and ``stat`` (concurrent removal) is skipped — the next read
    self-heals.
    """
    stats: list[PackageStat] = []
    for path in sorted(Path(package_dir).glob("*.aphelion.tar")):
        try:
            st = path.stat()
        except OSError:
            continue
        stats.append(PackageStat(name=path.name, size=st.st_size, mtime_ns=st.st_mtime_ns))
    return tuple(stats)


def current_package_files(package_dir: Path | str) -> tuple[str, ...]:
    """Sorted basenames of the ``.aphelion.tar`` files currently on disk."""
    return tuple(pkg.name for pkg in current_packages(package_dir))


def load_index(package_dir: Path | str) -> SubjectIndex | None:
    """Load + validate the persisted index, or ``None`` if absent/corrupt.

    A corrupt or schema-mismatched file returns ``None`` (the caller rebuilds)
    rather than raising — a derived cache must never be able to break a read.
    """
    path = index_path(package_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.warning("subject index unreadable (%s); will rebuild: %s", path, exc)
        return None

    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return None
        entries = tuple(
            IndexEntry(
                subject=str(item["subject"]),
                package_id=str(item["package_id"]),
                claim_id=str(item["claim_id"]),
                package_file=str(item["package_file"]),
            )
            for item in data["entries"]
        )
        packages = tuple(
            PackageStat(
                name=str(item["name"]),
                size=int(item["size"]),
                mtime_ns=int(item["mtime_ns"]),
            )
            for item in data["packages"]
        )
        built_at = float(data["built_at"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _log.warning("subject index corrupt (%s); will rebuild: %s", path, exc)
        return None
    return SubjectIndex(entries=entries, packages=packages, built_at=built_at)


def save_index(package_dir: Path | str, index: SubjectIndex) -> None:
    """Persist ``index`` atomically (temp file + ``os.replace``).

    The temp file is pid-suffixed so a read-path rebuild and an M6 ingest
    writing concurrently cannot clobber each other's partial file; whichever
    ``replace`` lands last wins, and the read path's stale-rebuild self-heals
    any divergence on the next query.
    """
    path = index_path(package_dir)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "built_at": index.built_at,
        "packages": [
            {"name": pkg.name, "size": pkg.size, "mtime_ns": pkg.mtime_ns}
            for pkg in index.packages
        ],
        "entries": [
            {
                "subject": entry.subject,
                "package_id": entry.package_id,
                "claim_id": entry.claim_id,
                "package_file": entry.package_file,
            }
            for entry in index.entries
        ],
    }
    tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# M6 ingest entry point (incremental write — the fast-path maintainer)
# ---------------------------------------------------------------------------


def update_index_for_package(
    package_dir: Path | str,
    *,
    package_file: str,
    package_id: str,
    claim_subjects: Iterable[tuple[str, str]],
) -> None:
    """Add/replace one package's entries in the index (called by M6 ingest).

    Args:
        package_dir: ``PARALLAX_APHELION_PACKAGE_DIR``.
        package_file: the ingested package's ``.aphelion.tar`` basename.
        package_id: the package's manifest id.
        claim_subjects: ``(claim_id, subject)`` for each claim. Claims with an
            empty/falsy subject are skipped (a subjectless claim is not
            R4-routable and has nothing to resolve against).

    The merge keeps existing entries for *other* packages that still exist on
    disk, drops any whose package vanished, and replaces this package's prior
    entries (idempotent re-ingest).

    Crucially, an *other* package's recorded identity is **carried forward from
    the existing index, not re-stat'd to its current on-disk value** (only the
    just-ingested package is stat'd fresh). Re-stamping every tar's current
    identity here would certify a since-changed other package as fresh while its
    carried entries are stale — so a later ``load_or_rebuild`` would see a
    matching identity set and never rebuild, serving stale subjects indefinitely
    (codex #74 round 2). By preserving the recorded identity, a changed other
    package no longer matches disk and the next read rebuilds. A package on disk
    but absent from the existing index is likewise left out of the identity set,
    forcing a rebuild that picks it up.
    """
    existing = load_index(package_dir)
    current = current_packages(package_dir)
    on_disk = {pkg.name for pkg in current}
    ingested_stat = next((pkg for pkg in current if pkg.name == package_file), None)

    prev_entries = existing.entries if existing else ()
    prev_packages = existing.packages if existing else ()

    base_entries = [
        entry
        for entry in prev_entries
        if entry.package_file != package_file and entry.package_file in on_disk
    ]
    # Carry the OTHER packages' recorded identities verbatim (see docstring).
    base_packages = [
        pkg for pkg in prev_packages if pkg.name != package_file and pkg.name in on_disk
    ]
    added = [
        IndexEntry(
            subject=subject,
            package_id=package_id,
            claim_id=claim_id,
            package_file=package_file,
        )
        for claim_id, subject in claim_subjects
        if subject
    ]
    combined = list(base_packages)
    if ingested_stat is not None:
        combined.append(ingested_stat)
    # Sort by name to match current_packages() ordering: load_or_rebuild compares
    # the identity tuples for exact equality, so an unsorted append would look
    # stale immediately after a clean update and force a needless full rebuild on
    # the next read (codex #74 round 3), undercutting the §8.4 fast path.
    packages = tuple(sorted(combined, key=lambda pkg: pkg.name))
    index = SubjectIndex(
        entries=tuple(base_entries) + tuple(added),
        packages=packages,
        built_at=time.time(),
    )
    save_index(package_dir, index)


# ---------------------------------------------------------------------------
# Read-path entry point (freshness-guaranteed load)
# ---------------------------------------------------------------------------


def load_or_rebuild(
    package_dir: Path | str,
    scan_fn: Callable[[], Iterable[IndexEntry]],
) -> SubjectIndex:
    """Return a fresh index, rebuilding synchronously on a cache miss.

    Freshness rule (spec §8.4): the index is fresh iff its recorded package
    identities equal the live filesystem's — same names, sizes, **and** mtimes.
    On any mismatch — missing file, corrupt file, a package added/removed, or a
    same-name content swap — the index is rebuilt from ``scan_fn`` **before
    returning**, so a subject present in new/changed on-disk content is never
    served stale-empty.

    Args:
        package_dir: ``PARALLAX_APHELION_PACKAGE_DIR``.
        scan_fn: zero-arg callable returning the full set of :class:`IndexEntry`
            for the current corpus (the router injects a safe unpack+project
            scan). It may raise — e.g. a corrupt package — and that propagates
            unchanged, matching the read path's abort-on-bad-package contract.

    Returns:
        A :class:`SubjectIndex` guaranteed consistent with the current corpus.
    """
    current = current_packages(package_dir)
    existing = load_index(package_dir)

    if existing is not None and existing.packages == current:
        INDEX_STALENESS.set(0.0)
        return existing

    if existing is None:
        trigger = "missing" if not index_path(package_dir).exists() else "corrupt"
        INDEX_STALENESS.set(0.0)
    else:
        trigger = "stale"
        INDEX_STALENESS.set(max(0.0, time.time() - existing.built_at))

    entries = tuple(scan_fn())
    rebuilt = SubjectIndex(entries=entries, packages=current, built_at=time.time())
    # Persisting is an optimisation; the in-memory index already serves this
    # query correctly, so a write failure must not break the read.
    try:
        save_index(package_dir, rebuilt)
    except OSError as exc:
        _log.warning("subject index persist failed after rebuild; serving in-memory: %s", exc)
    INDEX_REBUILD.labels(trigger=trigger).inc()
    return rebuilt
