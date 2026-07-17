"""Apex — Parallax → Aphelion claim export (the outbound direction of M6).

The M6 ingest pipeline (:mod:`parallax.apex.aphelion_ingest`) is the *inbound*
direction: ``.aphelion.tar`` → audit rows + subject index. This module is the
missing *outbound* half — it serializes a batch of Parallax claims into the
canonical Aphelion package layout the ingest reader + M7 public-read router
accept, then packs it into a byte-deterministic ``.aphelion.tar`` via
:func:`aphelion.packer.pack`.

Scope (deliberately minimal, product-importable):
  * NO HTTP route, NO CLI wiring, NO server changes — this is a library
    surface only. Wiring it into a route/CLI is a separate change.
  * NO signing. ``pack`` produces the unsigned canonical archive; attaching a
    ``signatures.jsonl`` + ``signers/<id>.json`` envelope (v0.5) is an operator
    / signing-service concern layered on top, exactly as the M6 ingest tests
    layer it. The ingest reader's ``verify_package(require_signed=True)`` gate
    therefore runs against a *signed* wrapping of this module's output.
  * Dependencies: Python stdlib + the ``aphelion`` package only.

Layout produced (per ``spec/packaging.md`` + ``spec/claim-frontmatter.md`` and,
crucially, per what the installed ``aphelion`` validators actually enforce):

  * ``claims/<claim_id>.md`` — one file per claim: canonical v0.3 YAML
    frontmatter between ``---`` fences (emitted via
    :func:`aphelion.yaml_canonical.emit_frontmatter`, keys ASCII-ascending)
    followed by the claim's markdown body.
  * ``manifest.json`` — canonical JSON manifest (format_version ``2.0``); one
    entry per claim with a recomputed sha256 ``hash``.
  * ``provenance.jsonl`` — one ``create`` event per claim.

Frontmatter subset: the exporter writes ``claim_id`` plus only the fields the
v0.3 conflict model (R1–R4) and the M7 read path consume — ``subject``,
``polarity``, ``valid_from``, ``valid_until``, ``supersedes``, and the
application-defined ``target_claim_id`` (a support/contradiction pointer that is
inert to the v0.3 validator but round-trips verbatim). Fields that are absent /
empty are OMITTED entirely rather than emitted as ``null`` / ``[]`` — the
canonical YAML parser (:func:`aphelion.yaml_canonical.parse_frontmatter`)
rejects an inline ``[]`` flow sequence, so ``emit_frontmatter``'s ``key: []``
form is not a safe round-trip and empty collections must never be written.

Determinism: given identical ``ExportClaim`` inputs and package metadata the
byte output is identical (``aphelion.packer.pack`` guarantees this for the
archive; this module guarantees it for the claim/manifest/provenance bytes it
feeds in — including a deterministic UUID-v7 derivation for provenance
``event_id`` values).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aphelion.canonical_json import dumps as canonical_dumps
from aphelion.canonical_json import normalize as canonical_normalize
from aphelion.packer import pack as aphelion_pack
from aphelion.yaml_canonical import emit_frontmatter

__all__ = [
    "ExportClaim",
    "ExportedPackage",
    "build_claim_markdown",
    "export_claims",
]

# Wire constants. ``format_version`` is the only value the aphelion validator
# gates on (``2.0`` is the sole supported wire schema); ``aphelion_spec_version``
# is informational semver.
_FORMAT_VERSION = "2.0"
_APHELION_SPEC_VERSION = "0.6.0"
_PROVENANCE_FILENAME = "provenance.jsonl"
_DEFAULT_CREATED_AT = "2026-01-01T00:00:00Z"
_DEFAULT_PRODUCER = "parallax-apex-export"
_DEFAULT_LICENSE = "Apache-2.0"
_DEFAULT_ACTOR = "parallax-apex-export"


@dataclass(frozen=True)
class ExportClaim:
    """One Parallax claim to serialize into an Aphelion package.

    ``claim_id`` and ``claim_instance_id`` are UUID v7 strings
    (``claim_instance_id`` MUST differ from ``claim_id`` — they occupy
    distinct identity spaces). ``claim_instance_id`` may be left ``None``, in
    which case a deterministic value is derived from ``claim_id`` at export
    time (convenient when re-exporting claims read back from a package, whose
    frontmatter does not carry the instance id).

    ``body`` is the markdown claim statement written after the frontmatter.
    The remaining fields are the v0.3 conflict-model surface; each is omitted
    from the frontmatter when ``None`` / empty. ``supersedes`` is lex-sorted on
    write (spec ``claim-frontmatter.md`` R4). ``target_claim_id`` is an
    application-defined support/contradiction pointer carried verbatim.
    """

    claim_id: str
    body: str
    claim_instance_id: str | None = None
    subject: str | None = None
    polarity: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    supersedes: tuple[str, ...] = ()
    target_claim_id: str | None = None


@dataclass(frozen=True)
class ExportedPackage:
    """Result of :func:`export_claims`."""

    tar_path: Path
    source_dir: Path
    package_id: str
    claim_ids: tuple[str, ...]


def _derive_uuid7(*parts: str) -> str:
    """Deterministically derive a syntactically valid UUID v7 from ``parts``.

    Used for provenance ``event_id`` values (and as the fallback
    ``claim_instance_id``) so an export is fully reproducible without a random
    source. The version nibble is forced to ``7`` and the variant nibble into
    ``{8,9,a,b}`` so the result matches the aphelion validator's
    ``UUID_V7_RE``; the remaining bits are sha256 of the joined parts.
    """
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    variant = format((int(digest[16], 16) & 0x3) | 0x8, "x")
    return (
        f"{digest[0:8]}-{digest[8:12]}-7{digest[13:16]}-"
        f"{variant}{digest[17:20]}-{digest[20:32]}"
    )


def build_claim_markdown(claim: ExportClaim) -> bytes:
    """Serialize one claim to canonical ``claims/<id>.md`` bytes.

    Emits ASCII-ascending YAML frontmatter (only the present fields) between
    ``---`` fences, followed by the claim body. The output is the exact inverse
    of :func:`aphelion.yaml_canonical.split_frontmatter` +
    :func:`~aphelion.yaml_canonical.parse_frontmatter`, so a round-trip through
    a package + the M7 read path re-serializes byte-for-byte.
    """
    frontmatter: dict[str, Any] = {"claim_id": claim.claim_id}
    if claim.subject is not None:
        frontmatter["subject"] = claim.subject
    if claim.polarity is not None:
        frontmatter["polarity"] = claim.polarity
    if claim.valid_from is not None:
        frontmatter["valid_from"] = claim.valid_from
    if claim.valid_until is not None:
        frontmatter["valid_until"] = claim.valid_until
    if claim.supersedes:
        # Non-empty only: an empty list would emit ``supersedes: []``, which the
        # canonical parser rejects as an unsupported inline flow sequence.
        frontmatter["supersedes"] = sorted(claim.supersedes)
    if claim.target_claim_id is not None:
        frontmatter["target_claim_id"] = claim.target_claim_id

    ordered = dict(sorted(frontmatter.items()))
    yaml_block = emit_frontmatter(ordered)
    document = f"---\n{yaml_block}---\n{claim.body}"
    return document.encode("utf-8")


def export_claims(
    claims: Sequence[ExportClaim],
    *,
    source_dir: Path | str,
    tar_path: Path | str,
    package_id: str,
    created_at: str = _DEFAULT_CREATED_AT,
    producer: str = _DEFAULT_PRODUCER,
    license: str = _DEFAULT_LICENSE,
    actor: str = _DEFAULT_ACTOR,
) -> ExportedPackage:
    """Materialize ``claims`` as an Aphelion package dir + pack it to a tar.

    Writes ``claims/<id>.md`` (one per claim), ``manifest.json``, and
    ``provenance.jsonl`` under ``source_dir``, then calls
    :func:`aphelion.packer.pack` to produce the canonical ``.aphelion.tar`` at
    ``tar_path``. ``pack`` re-validates + re-canonicalizes the manifest and
    recomputes every claim hash, so the resulting archive is the canonical
    form the ingest reader verifies.

    Args:
        claims: the claims to export (must be non-empty for the package to be
            ingestable — the M6 reader rejects an empty claim set).
        source_dir: directory to materialize the package tree in (created).
        tar_path: output path for the ``.aphelion.tar`` (parent created by pack).
        package_id: UUID v7 package identifier written to the manifest.
        created_at / producer / license / actor: manifest + provenance metadata.

    Returns:
        :class:`ExportedPackage` with the tar path, source dir, package id, and
        the ordered claim ids.
    """
    source = Path(source_dir)
    (source / "claims").mkdir(parents=True, exist_ok=True)

    manifest_claims: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for claim in claims:
        instance_id = claim.claim_instance_id or _derive_uuid7(
            package_id, claim.claim_id, "instance"
        )
        md_bytes = build_claim_markdown(claim)
        rel_path = f"claims/{claim.claim_id}.md"
        (source / rel_path).write_bytes(md_bytes)

        manifest_claims.append(
            {
                "claim_id": claim.claim_id,
                "claim_instance_id": instance_id,
                "hash": hashlib.sha256(md_bytes).hexdigest(),
                "path": rel_path,
                "state": "active",
            }
        )
        events.append(
            {
                "actor": actor,
                "claim_id": claim.claim_id,
                "claim_instance_id": instance_id,
                "event_id": _derive_uuid7(package_id, claim.claim_id, "create"),
                "event_type": "create",
                "timestamp": created_at,
            }
        )

    manifest = {
        "aphelion_spec_version": _APHELION_SPEC_VERSION,
        "claims": manifest_claims,
        "created_at": created_at,
        "format_version": _FORMAT_VERSION,
        "license": license,
        "package_id": package_id,
        "producer": producer,
        "provenance_path": _PROVENANCE_FILENAME,
    }
    (source / "manifest.json").write_bytes(
        canonical_dumps(canonical_normalize(manifest))
    )
    provenance_bytes = b"".join(
        canonical_dumps(canonical_normalize(event)) for event in events
    )
    (source / _PROVENANCE_FILENAME).write_bytes(provenance_bytes)

    out = aphelion_pack(source, Path(tar_path))
    return ExportedPackage(
        tar_path=out,
        source_dir=source,
        package_id=package_id,
        claim_ids=tuple(claim.claim_id for claim in claims),
    )
