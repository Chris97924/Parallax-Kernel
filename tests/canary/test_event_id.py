"""US-009.1 §3.1 criterion 1.1 — UUID v7 generator tests."""

from __future__ import annotations

import uuid

import pytest

from parallax.canary.event_id import is_uuid7, uuid7


def test_uuid7_is_valid_uuid_v7() -> None:
    eid = uuid7()
    assert isinstance(eid, uuid.UUID)
    assert eid.version == 7
    assert is_uuid7(eid)


def test_uuid7_is_unique_across_calls() -> None:
    sample = {str(uuid7()) for _ in range(2_000)}
    # 2k samples in a 74-bit random space → collision probability ≈ 0.
    assert len(sample) == 2_000


def test_uuid7_embeds_provided_timestamp() -> None:
    ms = 1_700_000_000_000
    eid = uuid7(_ms=ms, _rand=b"\x00" * 10)
    assert int.from_bytes(eid.bytes[:6], "big") == ms


def test_uuid7_orders_by_timestamp() -> None:
    """Two UUIDs minted at different ms values sort by timestamp prefix."""
    a = uuid7(_ms=1_000, _rand=b"\xff" * 10)
    b = uuid7(_ms=2_000, _rand=b"\x00" * 10)
    assert a.bytes[:6] < b.bytes[:6]


def test_uuid7_rejects_negative_timestamp() -> None:
    with pytest.raises(ValueError):
        uuid7(_ms=-1)


def test_uuid7_rejects_oversize_timestamp() -> None:
    with pytest.raises(ValueError):
        uuid7(_ms=1 << 48)


def test_uuid7_rejects_wrong_rand_length() -> None:
    with pytest.raises(ValueError):
        uuid7(_rand=b"\x00" * 9)


def test_is_uuid7_rejects_v4() -> None:
    assert is_uuid7(uuid.uuid4()) is False


def test_is_uuid7_accepts_string_form() -> None:
    eid = uuid7()
    assert is_uuid7(str(eid)) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        "12345678-1234-1234-1234-1234567890ab",  # version=1
        None,
        42,
    ],
)
def test_is_uuid7_rejects_garbage(value: object) -> None:
    assert is_uuid7(value) is False  # type: ignore[arg-type]
