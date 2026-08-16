"""Mutation-hardening for ``parallax.apex.aphelion_export`` (overnight-20260816 S8).

Additive companion to ``test_aphelion_roundtrip.py``. Every test below was
written against a semantic mutant that the pre-existing suite let through.

The existing file is a round-trip harness: it exports a corpus, ingests it back,
and compares. That is strong on the paths a *good* corpus takes and weak
everywhere the exporter's guards and metadata plumbing live.

  * **The guards are pinned only at their centre, not their edges.** The
    multi-line rejection is parametrized over CR and LF, so a guard narrowed
    from ``str.splitlines()`` to those two characters keeps passing while the
    other Unicode line boundaries — which ``parse_frontmatter`` splits on too —
    sail through. The claim-id allowlist is exercised with path-traversal and
    uppercase inputs, never with a *well-formed UUID of the wrong version or
    variant*, so the two nibbles that make it a v7 check are unobserved.

  * **All-or-nothing is proven for one guard only.** The line-break guard has an
    explicit "does not delete stale claims" case; the claim-id guard does not,
    and its existing case exports a single bad claim into a fresh directory —
    where validating late is indistinguishable from validating early.

  * **The written artifacts are never read.** ``manifest.json`` and
    ``provenance.jsonl`` are checked only through what survives a re-ingest, so
    the manifest hash's subject, the caller-supplied ``actor`` and
    ``claim_instance_id``, and the frontmatter key ordering are all unasserted.

  * **The cleanup's blast radius.** The stale-``*.md`` sweep is deliberately
    scoped to direct children of the owned ``claims/`` dir; nothing observes
    that it stops there.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from aphelion.errors import SchemaError
from aphelion.yaml_canonical import (
    emit_frontmatter,
    parse_frontmatter,
    split_frontmatter,
)

from parallax.apex.aphelion_export import (
    _CLAIM_ID_RE,
    AphelionExportError,
    ExportClaim,
    _derive_uuid7,
    build_claim_markdown,
    export_claims,
)

_PACKAGE_ID = "0190ab63-5f8a-7a61-9b14-ffaa20c1e000"
_CID_A = "0190ab63-5f8a-7a61-9b14-ffaa20c1d00a"
_CID_B = "0190ab63-5f8a-7a61-9b14-ffaa20c1d00b"
_IID_A = "0190ab63-5f8a-7a61-9b14-ffaa20c1e00a"
_IID_B = "0190ab63-5f8a-7a61-9b14-ffaa20c1e00b"


def _claim(claim_id: str = _CID_A, **kwargs: object) -> ExportClaim:
    fields: dict[str, object] = {
        "claim_id": claim_id,
        "claim_instance_id": _IID_A,
        "body": "Body statement.\n",
        "subject": "subject:s",
    }
    fields.update(kwargs)
    return ExportClaim(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The line-break guard's boundary set
# ---------------------------------------------------------------------------


# The line boundaries ``str.splitlines()`` recognises beyond CR and LF. The
# pre-existing suite parametrizes the guard over LF / CR / CRLF only, so these
# are exactly the characters a guard narrowed to those two would wave through.
_NON_CRLF_LINE_BOUNDARIES = [
    ("line separator", "\u2028"),
    ("paragraph separator", "\u2029"),
    ("next line", "\x85"),
    ("vertical tab", "\v"),
    ("form feed", "\f"),
    ("file separator", "\x1c"),
    ("group separator", "\x1d"),
    ("record separator", "\x1e"),
]


@pytest.mark.parametrize(("name", "char"), _NON_CRLF_LINE_BOUNDARIES)
def test_line_break_guard_covers_the_whole_splitlines_boundary_set(
    name: str, char: str
) -> None:
    """The guard mirrors ``str.splitlines()``, not just CR and LF.

    ``parse_frontmatter`` reads each frontmatter key from one physical line by
    iterating ``text.splitlines()``, so every boundary that function recognises
    splits a value on the read side too. The companion test below shows each of
    these characters really does produce a document the production parser
    refuses, so a guard that knows only ``\\n`` and ``\\r`` emits eight further
    classes of package that ingest rejects \u2014 the issue #95 failure.
    """
    assert f"a{char}b".splitlines() != [f"a{char}b"], f"{name} must split lines"

    with pytest.raises(AphelionExportError, match="line break"):
        build_claim_markdown(_claim(subject=f"before{char}after"))


@pytest.mark.parametrize(("name", "char"), _NON_CRLF_LINE_BOUNDARIES)
def test_every_rejected_line_boundary_really_breaks_the_production_parser(
    name: str, char: str
) -> None:
    """Evidence the guard is not over-broad \u2014 and a fence against relaxing it.

    Emitting the value the way ``build_claim_markdown`` does, but without the
    guard in front of it, yields a quoted scalar spanning two physical lines,
    and the production reader rejects the document as an unterminated scalar.
    That is the concrete harm the guard prevents for each character, so this
    also turns red if the boundary set is later trimmed as "over-strict".
    """
    frontmatter = {"claim_id": _CID_A, "subject": f"before{char}after"}
    document = f"---\n{emit_frontmatter(frontmatter)}---\nBody statement.\n"

    with pytest.raises(SchemaError, match="unterminated quoted scalar"):
        yaml_part, _ = split_frontmatter(document)
        parse_frontmatter(yaml_part)


# ---------------------------------------------------------------------------
# The claim-id allowlist is a UUID *v7* allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "bad_id"),
    [
        ("uuid v1", "0190ab63-5f8a-1a61-9b14-ffaa20c1d00d"),
        ("uuid v4", "0190ab63-5f8a-4a61-9b14-ffaa20c1d00d"),
        ("uuid v8", "0190ab63-5f8a-8a61-9b14-ffaa20c1d00d"),
    ],
)
def test_claim_id_must_be_uuid_version_7(
    label: str, bad_id: str, tmp_path: Path
) -> None:
    """A well-formed UUID of the wrong version is still rejected.

    The allowlist is not merely a path-traversal filter — it is the read side's
    bar. ``aphelion.validator`` rejects a non-v7 ``claim_id``, so accepting one
    here produces a package the ingest reader refuses.
    """
    with pytest.raises(AphelionExportError):
        export_claims(
            [_claim(claim_id=bad_id)],
            source_dir=tmp_path / "src",
            tar_path=tmp_path / "out.aphelion.tar",
            package_id=_PACKAGE_ID,
        )
    assert not list(tmp_path.rglob("*.md"))


def test_claim_id_must_carry_the_rfc4122_variant_nibble(tmp_path: Path) -> None:
    """The variant nibble must be one of ``{8,9,a,b}``.

    Same reason as the version nibble: ``aphelion.validator``'s ``UUID_V7_RE``
    checks it, so a ``c``-variant id exports a package that cannot be ingested.
    """
    with pytest.raises(AphelionExportError):
        export_claims(
            [_claim(claim_id="0190ab63-5f8a-7a61-cb14-ffaa20c1d00d")],
            source_dir=tmp_path / "src",
            tar_path=tmp_path / "out.aphelion.tar",
            package_id=_PACKAGE_ID,
        )
    assert not list(tmp_path.rglob("*.md"))


# ---------------------------------------------------------------------------
# Canonical frontmatter key order
# ---------------------------------------------------------------------------


def test_frontmatter_keys_are_emitted_ascii_ascending() -> None:
    """``emit_frontmatter`` preserves the caller's mapping order, so the sort
    here *is* the canonical key order.

    Losing it leaves fields in field-declaration order (``subject`` before
    ``polarity``), which breaks the canonical form the read side's key-order
    validator expects and makes the byte output depend on the dataclass layout.
    """
    text = build_claim_markdown(
        _claim(
            polarity="affirm",
            valid_from="2026-01-01T00:00:00Z",
            valid_until="2026-06-01T00:00:00Z",
            target_claim_id=_CID_B,
            supersedes=(_CID_B,),
        )
    ).decode("utf-8")

    lines = text.splitlines()
    assert lines[0] == "---"
    fence_end = lines.index("---", 1)
    keys = [
        line.split(":", 1)[0]
        for line in lines[1:fence_end]
        if not line.startswith((" ", "\t"))
    ]
    assert keys == [
        "claim_id",
        "polarity",
        "subject",
        "supersedes",
        "target_claim_id",
        "valid_from",
        "valid_until",
    ]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Deterministic UUID v7 derivation
# ---------------------------------------------------------------------------


def test_derived_uuid7_separates_its_parts() -> None:
    """Parts are joined on a separator that cannot occur in the inputs.

    Concatenating them makes the derivation ambiguous at the part boundary:
    two different ``(package_id, claim_id, tag)`` triples that concatenate to
    the same string derive the *same* id, so two distinct claims could be
    handed one provenance ``event_id``.
    """
    assert _derive_uuid7("a", "bc") != _derive_uuid7("ab", "c")
    assert _derive_uuid7("pkg", "claim", "create") != _derive_uuid7(
        "pkg", "claim", "instance"
    )


def test_derived_uuid7_is_always_a_syntactically_valid_uuid_v7() -> None:
    """The version and variant nibbles are forced, not inherited from the digest.

    Only 4 of the 16 possible digest nibbles are legal RFC 4122 variants, so an
    unforced derivation produces ids the aphelion manifest validator rejects
    for roughly three exports in four.
    """
    for n in range(64):
        for tag in ("instance", "create"):
            derived = _derive_uuid7(f"package-{n}", f"claim-{n}", tag)
            assert _CLAIM_ID_RE.fullmatch(derived), (n, tag, derived)


# ---------------------------------------------------------------------------
# The owned-subtree cleanup and its blast radius
# ---------------------------------------------------------------------------


def test_stale_cleanup_does_not_recurse_below_the_owned_claims_dir(
    tmp_path: Path,
) -> None:
    """The sweep removes exactly the files this function writes.

    It writes ``claims/<id>.md`` and nothing deeper, so recursing would delete
    content in a nested directory the exporter does not own and never created.
    """
    src = tmp_path / "src"
    export_claims(
        [_claim(claim_id=_CID_A)],
        source_dir=src,
        tar_path=tmp_path / "one.aphelion.tar",
        package_id=_PACKAGE_ID,
    )
    first = src / "claims" / f"{_CID_A}.md"
    assert first.exists()

    nested = src / "claims" / "nested"
    nested.mkdir()
    foreign = nested / "keep-me.md"
    foreign.write_text("not the exporter's file", encoding="utf-8")

    export_claims(
        [_claim(claim_id=_CID_B)],
        source_dir=src,
        tar_path=tmp_path / "two.aphelion.tar",
        package_id=_PACKAGE_ID,
    )

    assert not first.exists(), "a direct stale claim file must still be swept"
    assert foreign.read_text(encoding="utf-8") == "not the exporter's file"


def test_claim_id_validation_precedes_every_write(tmp_path: Path) -> None:
    """The claim-id guard runs in the prepare pass, before any filesystem work.

    Validating inside the write loop still raises, but only after the stale
    sweep has deleted the previous export's claims and the earlier claims of
    *this* export have been written — leaving the package dir holding a partial
    export that matches no manifest.
    """
    src = tmp_path / "src"
    export_claims(
        [_claim(claim_id=_CID_A)],
        source_dir=src,
        tar_path=tmp_path / "one.aphelion.tar",
        package_id=_PACKAGE_ID,
    )
    prior = src / "claims" / f"{_CID_A}.md"
    assert prior.exists()

    second_tar = tmp_path / "two.aphelion.tar"
    with pytest.raises(AphelionExportError):
        export_claims(
            [_claim(claim_id=_CID_B), _claim(claim_id="../evil")],
            source_dir=src,
            tar_path=second_tar,
            package_id=_PACKAGE_ID,
        )

    assert prior.exists(), "a rejected export must not delete the prior export"
    assert not (src / "claims" / f"{_CID_B}.md").exists(), "nothing may be written"
    assert not second_tar.exists()
    assert not list(tmp_path.rglob("*evil*"))


# ---------------------------------------------------------------------------
# The written manifest / provenance artifacts
# ---------------------------------------------------------------------------


def test_manifest_hash_covers_the_written_claim_file_bytes(tmp_path: Path) -> None:
    """``hash`` digests the whole ``claims/<id>.md``, frontmatter included.

    Digesting only the body would leave every frontmatter field — subject,
    polarity, validity window — outside the integrity check the ingest reader
    verifies the package with.
    """
    src = tmp_path / "src"
    export_claims(
        [_claim(polarity="affirm")],
        source_dir=src,
        tar_path=tmp_path / "out.aphelion.tar",
        package_id=_PACKAGE_ID,
    )

    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    (entry,) = manifest["claims"]
    written = (src / entry["path"]).read_bytes()

    assert entry["path"] == f"claims/{_CID_A}.md"
    assert entry["hash"] == hashlib.sha256(written).hexdigest()
    assert b"subject:" in written, "the digested bytes include the frontmatter"


def test_provenance_records_the_caller_supplied_actor(tmp_path: Path) -> None:
    """``actor`` is a parameter, so it must reach the provenance event.

    Hard-coding the default silently discards the caller's attribution while
    still producing a package that ingests cleanly — provenance that names the
    wrong party is worse than none.
    """
    src = tmp_path / "src"
    export_claims(
        [_claim()],
        source_dir=src,
        tar_path=tmp_path / "out.aphelion.tar",
        package_id=_PACKAGE_ID,
        actor="ops@example.test",
    )

    lines = (src / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]

    assert [event["actor"] for event in events] == ["ops@example.test"]
    assert events[0]["event_type"] == "create"
    assert events[0]["claim_id"] == _CID_A


def test_caller_supplied_claim_instance_id_is_carried_verbatim(
    tmp_path: Path,
) -> None:
    """The derivation is a *fallback* for when the caller has no instance id.

    Overriding a supplied one breaks re-export of claims read back from an
    existing package: the instance id is a distinct identity that provenance
    events chain on, so replacing it detaches the re-export from that chain.
    """
    src = tmp_path / "src"
    supplied = "0190ab63-5f8a-7a61-9b14-ffaa20c1efff"
    export_claims(
        [_claim(claim_instance_id=supplied)],
        source_dir=src,
        tar_path=tmp_path / "out.aphelion.tar",
        package_id=_PACKAGE_ID,
    )

    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    (entry,) = manifest["claims"]
    events = [
        json.loads(line)
        for line in (src / "provenance.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert entry["claim_instance_id"] == supplied
    assert events[0]["claim_instance_id"] == supplied


def test_returned_claim_ids_preserve_the_input_order(tmp_path: Path) -> None:
    """``claim_ids`` is documented as the *ordered* claim ids.

    It is the caller's handle back onto the batch they passed in; re-sorting it
    silently breaks any caller zipping it against their own claim list.
    """
    package = export_claims(
        [
            _claim(claim_id=_CID_B, claim_instance_id=_IID_B),
            _claim(claim_id=_CID_A, claim_instance_id=_IID_A),
        ],
        source_dir=tmp_path / "src",
        tar_path=tmp_path / "out.aphelion.tar",
        package_id=_PACKAGE_ID,
    )

    assert package.claim_ids == (_CID_B, _CID_A)
