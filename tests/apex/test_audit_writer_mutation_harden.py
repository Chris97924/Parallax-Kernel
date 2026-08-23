"""Mutation-hardening for ``parallax.apex.audit_writer`` (land-20260823 wave 4, S3).

Additive companion to ``tests/apex/test_audit_writer.py``. Every test below was
written against a semantic mutant that the pre-existing suite let through.

The existing file validates each rule from the outside in, one field at a time.
That leaves four blind spots:

  * **Bidirectional rules are only proven in one direction.**
    ``_validate_optional_pairing`` documents itself as bidirectional, and the
    "present without the right outcome" half has cases. The other half — an
    ``outcome`` that *requires* a field and does not have it — does not: the
    only ``divergence`` / ``error`` rows in the suite are well-formed ones.
    Relaxing ``or`` to ``and`` in the divergence check, or deleting the
    reason-code requirement outright, keeps the suite green while a divergence
    row lands with half its evidence missing.

  * **String guards are exercised at the centre of the wrong region.**
    ``reason_code`` is rejected for a namespace that appears nowhere in the
    allowlist, so ``startswith`` relaxed to ``in`` survives. ``ts`` is rejected
    for values that fail the *shape* check, so the ``strptime`` call behind it
    — the only thing that separates a real date from twenty well-arranged
    characters — is never reached by a failing input. The sha256 pattern is
    exercised with too-short and uppercase values, never with a valid digest
    plus trailing junk, so the ``$`` anchor is unobserved.

  * **An allowlist is only ever tested through one of its entries.** The
    empty-string exemption covers exactly ``signer_id`` and
    ``signer_manifest_digest``; the suite checks one field on each side of
    that line, so a third name drifting into the exempt tuple is invisible.
    Same for the six reserved ``reason_code`` prefixes.

  * **The enum constants are a schema, not just a validator input.**
    ``audit_db`` generates the ``audit_row`` CHECK clauses from
    ``OUTCOME_VALUES`` / ``SOURCE_VALUES`` at import time, so widening either
    set silently widens the database's own constraint. Nothing pins their
    contents.

Expected values are literals: field names, prefixes and enum members are
written out rather than derived from the module's own constants, which is what
lets these tests notice a constant being edited.
"""

from __future__ import annotations

from typing import Any

import pytest

from parallax.apex.audit_writer import (
    OUTCOME_VALUES,
    REASON_CODE_PREFIXES,
    SOURCE_VALUES,
    AuditRowValidationError,
    canonicalize_row,
)

pytestmark = pytest.mark.unit


def _valid_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_id": "0193e2b1-0001-7000-8000-000000000001",
        "envelope_message_id": "b3d7e2a1-4f8c-4b9d-8e3a-12c456789abc",
        "outcome": "hit",
        "package_id": "0193ef00-0001-7000-8000-000000000005",
        "session_id": "sess-2026-05-09-001",
        "signer_id": "chris@aphelion-graph",
        "signer_manifest_digest": "a" * 64,
        "source": "aphelion",
        "ts": "2026-05-09T14:23:11Z",
    }
    base.update(overrides)
    return base


def _divergence_row(**overrides: Any) -> dict[str, Any]:
    row = _valid_row(
        outcome="divergence",
        aphelion_hash="b" * 64,
        local_hash="c" * 64,
        reason_code="claim.hash_divergence",
    )
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Optional pairing — the "outcome requires the field" direction
# ---------------------------------------------------------------------------


class TestOptionalPairingRequiresDirection:
    """A divergence row without its evidence is worse than no row at all.

    The audit chain's whole claim is that a recorded divergence can be
    re-checked later. A row that says "the two sides disagreed" without
    carrying both hashes cannot be re-checked by anyone, and it is exactly what
    the ``or`` in the presence test prevents.
    """

    def test_divergence_without_local_hash_rejected(self) -> None:
        row = _divergence_row()
        del row["local_hash"]
        with pytest.raises(
            AuditRowValidationError, match="requires both aphelion_hash and local_hash"
        ):
            canonicalize_row(row)

    def test_divergence_without_aphelion_hash_rejected(self) -> None:
        row = _divergence_row()
        del row["aphelion_hash"]
        with pytest.raises(
            AuditRowValidationError, match="requires both aphelion_hash and local_hash"
        ):
            canonicalize_row(row)

    def test_divergence_with_neither_hash_rejected(self) -> None:
        row = _divergence_row()
        del row["aphelion_hash"]
        del row["local_hash"]
        with pytest.raises(AuditRowValidationError):
            canonicalize_row(row)

    def test_error_outcome_without_reason_code_rejected(self) -> None:
        """An ``error`` row with no reason code says nothing at all."""
        row = _valid_row(outcome="error")
        with pytest.raises(AuditRowValidationError, match="requires reason_code"):
            canonicalize_row(row)

    def test_divergence_without_reason_code_rejected(self) -> None:
        row = _divergence_row()
        del row["reason_code"]
        with pytest.raises(AuditRowValidationError, match="requires reason_code"):
            canonicalize_row(row)


# ---------------------------------------------------------------------------
# reason_code namespace — a prefix, not a substring
# ---------------------------------------------------------------------------


class TestReasonCodeNamespace:
    @pytest.mark.parametrize(
        "reason_code",
        [
            "xxpkg.corrupt",
            "not-a-signer.mismatch",
            "namespace:cache.miss",
            " disk.full",
        ],
    )
    def test_reserved_namespace_must_be_a_prefix(self, reason_code: str) -> None:
        """Containing a reserved namespace is not the same as being in it.

        The pre-existing rejection uses a namespace that appears nowhere in the
        allowlist, so ``startswith`` relaxed to ``in`` is invisible — and under
        that relaxation an arbitrary vendor string is accepted as long as it
        happens to mention one of the six namespaces anywhere inside it, which
        defeats the point of reserving them. Leading whitespace is the same
        failure in miniature.
        """
        row = _valid_row(outcome="error", reason_code=reason_code)
        with pytest.raises(AuditRowValidationError, match="reserved namespace"):
            canonicalize_row(row)

    @pytest.mark.parametrize(
        "prefix",
        ["pkg.", "signer.", "cache.", "network.", "disk.", "claim."],
    )
    def test_every_reserved_prefix_is_accepted(self, prefix: str) -> None:
        """All six namespaces, spelled out.

        The suite only ever builds reason codes from one of them, so five of
        the six could be deleted from the tuple without a single failure —
        and the symptom would be a production error row rejected at write
        time, on the path that is already handling a failure.
        """
        row = _valid_row(outcome="error", reason_code=f"{prefix}something")
        assert canonicalize_row(row).data["reason_code"] == f"{prefix}something"


# ---------------------------------------------------------------------------
# ts — the calendar check behind the shape check
# ---------------------------------------------------------------------------


class TestTimestampCalendarValidation:
    @pytest.mark.parametrize(
        "ts",
        [
            "2026-13-45T25:61:61Z",  # every component out of range
            "2026-02-30T00:00:00Z",  # a date that does not exist
            "2026-05-09 14:23:11Z",  # space where the T belongs
            "2026-0509T14:23:110Z",  # 20 chars, Z suffix, wrong layout
        ],
    )
    def test_well_shaped_but_impossible_timestamps_rejected(self, ts: str) -> None:
        """Twenty characters ending in ``Z`` is a shape, not a timestamp.

        Every pre-existing ``ts`` rejection fails the length/suffix guard, so
        the ``strptime`` behind it is never reached by an input that should
        fail. Delete that call and these all become valid audit rows —
        timestamps that no reader can order, on the one column the audit table
        indexes for time-range queries.
        """
        with pytest.raises(AuditRowValidationError, match="ts"):
            canonicalize_row(_valid_row(ts=ts))

    @pytest.mark.parametrize(
        "ts",
        ["2026-5-9T14:23:11Z", "2026-05-9T4:23:11Z", "2026-5-09T14:3:11Z"],
    )
    def test_single_digit_components_rejected_by_the_length_check(
        self, ts: str
    ) -> None:
        """The 20-char check is not redundant with ``strptime``.

        ``%m``, ``%d``, ``%H``, ``%M`` and ``%S`` each accept *one or two*
        digits, so ``"2026-5-9T14:23:11Z"`` parses cleanly into exactly the
        right moment — it is simply eighteen characters long, and only the
        explicit length comparison rejects it. Every pre-existing ``ts`` case
        is either the correct length or unparseable, which makes the two
        guards look interchangeable; they are not. Drop the length half and
        these rows reach ``audit_row``, where the column's own
        ``ts LIKE '____-__-__T__:__:__Z'`` CHECK rejects them — turning a
        clean validation error into a failed INSERT at the end of the write
        path, after the caller has already been told the row is good.
        """
        with pytest.raises(AuditRowValidationError, match="20 chars"):
            canonicalize_row(_valid_row(ts=ts))

    def test_valid_leap_day_accepted(self) -> None:
        """The calendar check must not be a blanket rejection either."""
        row = canonicalize_row(_valid_row(ts="2028-02-29T23:59:59Z"))
        assert row.data["ts"] == "2028-02-29T23:59:59Z"


# ---------------------------------------------------------------------------
# sha256 hex — the closing anchor
# ---------------------------------------------------------------------------


class TestSha256HexAnchoring:
    @pytest.mark.parametrize(
        "field", ["signer_manifest_digest", "aphelion_hash", "local_hash"]
    )
    def test_digest_with_trailing_junk_rejected(self, field: str) -> None:
        """64 valid hex chars followed by anything is not a sha256 digest.

        ``re.match`` anchors the start on its own, so the ``$`` in the pattern
        is doing all the work at the other end — and every pre-existing case
        (too short, uppercase) fails on characters the pattern rejects
        anyway. A digest with a suffix would flow into ``audit_row``, where
        the DB's own ``length(...) = 64`` CHECK would then reject the write at
        the very end of the pipeline instead of at validation.
        """
        oversized = "a" * 64 + "deadbeef"
        row = _divergence_row() if field != "signer_manifest_digest" else _valid_row()
        row[field] = oversized
        with pytest.raises(AuditRowValidationError, match="SHA-256 hex"):
            canonicalize_row(row)


# ---------------------------------------------------------------------------
# The empty-string exemption is exactly two fields wide
# ---------------------------------------------------------------------------


class TestEmptyStringExemption:
    @pytest.mark.parametrize(
        "field",
        [
            "claim_id",
            "envelope_message_id",
            "outcome",
            "package_id",
            "session_id",
            "source",
            "ts",
        ],
    )
    def test_empty_string_rejected_for_every_non_exempt_field(
        self, field: str
    ) -> None:
        """Seven fields, named one by one.

        The pre-existing case proves the rule for a single field, so a third
        name drifting into the exemption tuple is invisible. An empty
        ``claim_id`` or ``session_id`` is an audit row that cannot be traced
        back to anything — which is the one thing an audit row has to do.
        """
        with pytest.raises(AuditRowValidationError):
            canonicalize_row(_valid_row(**{field: ""}))

    @pytest.mark.parametrize("field", ["signer_id", "signer_manifest_digest"])
    def test_empty_string_accepted_for_the_two_exempt_fields(
        self, field: str
    ) -> None:
        """The exemption is deliberate: unsigned Aphelion packages.

        Pinned from the other side so a mutant that *narrows* the tuple —
        making the exemption stricter — is caught too. Narrowing would reject
        every unsigned package at ingest.
        """
        row = canonicalize_row(_valid_row(**{field: ""}))
        assert row.data[field] == ""


# ---------------------------------------------------------------------------
# The enum constants are the database's CHECK clause
# ---------------------------------------------------------------------------


class TestEnumConstants:
    def test_outcome_values_are_exactly_the_four_spec_values(self) -> None:
        """``audit_db`` renders these into the ``audit_row`` DDL at import.

        Widening the set silently widens the column constraint on every new
        database; narrowing it makes an existing database's rows unwritable.
        The pre-existing drift test walks the set and looks each member up in
        the DDL, which holds for any contents at all.
        """
        assert OUTCOME_VALUES == frozenset({"hit", "miss", "divergence", "error"})

    def test_source_values_are_exactly_the_two_spec_values(self) -> None:
        assert SOURCE_VALUES == frozenset({"aphelion", "parallax"})

    def test_reason_code_prefixes_are_exactly_the_six_reserved_namespaces(
        self,
    ) -> None:
        assert REASON_CODE_PREFIXES == (
            "pkg.",
            "signer.",
            "cache.",
            "network.",
            "disk.",
            "claim.",
        )


# ---------------------------------------------------------------------------
# canonicalize_row does not touch the caller's mapping
# ---------------------------------------------------------------------------


class TestCanonicalizeRowInputPurity:
    def test_caller_mapping_is_not_mutated(self) -> None:
        """"The row is **not** mutated" — asserted, not just documented.

        The None-stripping is done by building a new dict. Doing it in place
        would be a smaller diff and pass every existing test, while quietly
        editing a dict the caller still holds — and the caller here is the
        ingest path, which reuses its row builder across a whole package.
        """
        raw = _valid_row(reason_code=None, outcome="hit")
        snapshot = dict(raw)
        canonicalize_row(raw)
        assert raw == snapshot
        assert "reason_code" in raw

    def test_multiple_unknown_fields_report_the_first_in_sorted_order(self) -> None:
        """A deterministic error message for a deterministic input.

        The pre-existing case adds exactly one unknown key, where "sorted
        first" and "whichever the set yields" are the same string. With two,
        set iteration order decides — and that varies with PYTHONHASHSEED, so
        the mutant produces a message that changes between runs of the same
        failing deploy.
        """
        raw = _valid_row()
        raw["zulu_field"] = 1
        raw["alpha_field"] = 2
        with pytest.raises(AuditRowValidationError, match="'alpha_field'"):
            canonicalize_row(raw)
