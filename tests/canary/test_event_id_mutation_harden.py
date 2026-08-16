"""Mutation-hardening for ``parallax.canary.event_id`` (overnight-20260816 S10).

Companion to ``test_event_id.py``, which pins the *properties* of a UUID v7
(version nibble, uniqueness, timestamp ordering, the three argument guards) but
never the *layout*. Because every existing test either feeds an all-zero /
all-ones ``_rand`` or reads back only ``bytes[:6]``, the bit-twiddling between
the timestamp and the tail is unpinned end to end, and each test below was
written against a semantic mutant that survived the suite:

  * ``rand_a = ((rand[0] & 0x0F) << 8) | rand[1]`` reduced to ``0`` — dropping
    12 of the 74 random bits. ``test_uuid7_is_unique_across_calls`` draws 2 000
    samples from what is still a 62-bit space, so it stays green; nothing else
    looks at bytes 6-7 at all.
  * the variant mask ``& 0x3F`` narrowed to ``& 0x2F``. Every existing
    assertion stays green — the variant is still 0b10 and the version is still
    7 — while one bit of ``rand_b`` is silently pinned to zero.
  * ``_now_ms``'s ``// 1_000_000`` rescaled. Nothing asserts that a *freshly
    minted* id carries the current time; the only timestamp assertions inject
    ``_ms`` explicitly, which bypasses ``_now_ms`` entirely.
  * the range guards moved off their boundaries: ``ms < 0`` → ``ms <= 0``
    rejects the epoch, and ``ms >= (1 << 48)`` → ``ms >= (1 << 48) - 1`` rejects
    the largest legal timestamp. The existing tests only check the *reject*
    side, so an over-eager guard is invisible.
  * ``len(rand) != 10`` → ``len(rand) < 10``, which silently truncates an
    oversize ``_rand`` instead of rejecting it.

Two further mutations — dropping the ``& 0x0F`` version mask, and widening the
variant mask to ``& 0x7F`` — are already caught, because a ``uuid7()`` built
from real random bytes then fails ``is_uuid7`` and takes the idempotency handler
down with it. They are still pinned deterministically below: the version-mask
mutant is caught by chance on 15 draws in 16 and the variant-mask mutant on
roughly one in two, so with a single ``uuid7()`` call as the witness,
``test_uuid7_is_valid_uuid_v7`` is closer to a coin flip than to a test. The
cases here use fixed ``_rand`` values and fail every run.

The golden vector below is derived from RFC 9562 §5.7 by hand, field by field,
rather than by running the implementation — otherwise it would only assert that
the code agrees with itself.
"""

from __future__ import annotations

import uuid

import pytest

from parallax.canary import event_id as event_id_mod
from parallax.canary.event_id import is_uuid7, uuid7

# --- Golden vector, derived from RFC 9562 §5.7 -----------------------------
# Inputs:
_GOLDEN_MS = 1_700_000_000_000  # 0x018BCFE56800
_GOLDEN_RAND = bytes([0xAB, 0xCD, 0xEF, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])
# Field-by-field derivation:
#   unix_ts_ms (48 bits, big-endian) .... 01 8b cf e5 68 00
#   ver (4 bits) ........................ 0x7
#   rand_a (12 bits) = low 12 bits of the first two random bytes
#                      (0xAB, 0xCD) ..... 0xBCD      -> bytes 6-7 = 7b cd
#   var (2 bits) = 0b10
#   rand_b (62 bits) = low 6 bits of 0xEF (= 0x2F) then the last seven bytes
#                      0b10 << 6 | 0x2F . 0xAF      -> byte 8    = af
#                      tail ............. 01 02 03 04 05 06 07
_GOLDEN_UUID = "018bcfe5-6800-7bcd-af01-020304050607"


class _FrozenClock:
    """Stand-in for ``time`` with both clock sources pinned, to different values.

    ``_now_ms`` is specified to read the *epoch* clock. Giving ``monotonic_ns``
    a visibly unrelated value means a generator that reaches for the wrong
    source produces an obviously wrong millisecond rather than a plausible one.
    """

    def __init__(self, *, epoch_ns: int, monotonic_ns: int) -> None:
        self._epoch_ns = epoch_ns
        self._monotonic_ns = monotonic_ns

    def time_ns(self) -> int:
        return self._epoch_ns

    def monotonic_ns(self) -> int:
        return self._monotonic_ns


# ===========================================================================
# Byte layout (RFC 9562 §5.7)
# ===========================================================================


@pytest.mark.unit
class TestByteLayoutMatchesRFC9562:
    def test_golden_vector(self) -> None:
        """Every field lands in the byte the spec assigns it."""
        assert str(uuid7(_ms=_GOLDEN_MS, _rand=_GOLDEN_RAND)) == _GOLDEN_UUID

    def test_rand_a_carries_the_first_two_random_bytes(self) -> None:
        """Bytes 6-7 are ``0x7`` followed by the low 12 bits of ``rand[0:2]``.

        Kills the mutant that drops ``rand_a`` (bytes 6-7 become ``70 00``) and
        the one that omits the ``& 0x0F`` mask (0xAB's high nibble would OR into
        the version field).
        """
        eid = uuid7(_ms=_GOLDEN_MS, _rand=_GOLDEN_RAND)
        assert eid.bytes[6] == 0x7B
        assert eid.bytes[7] == 0xCD

    def test_version_nibble_survives_an_all_ones_first_random_byte(self) -> None:
        """``rand[0] = 0xFF`` must not leak into the version field."""
        eid = uuid7(_ms=_GOLDEN_MS, _rand=b"\xff" * 10)
        assert eid.version == 7
        assert is_uuid7(eid) is True
        assert eid.bytes[6] >> 4 == 0x7

    def test_variant_bits_survive_an_all_ones_third_random_byte(self) -> None:
        """``rand[2] = 0xFF`` must be masked to six bits, leaving variant 0b10.

        Widening the mask to ``& 0x7F`` lets bit 6 through and turns the variant
        into 0b11, which Python then reports as a non-RFC-4122 UUID. The
        existing suite only catches that on the ~50 % of random draws where the
        bit happens to be set — this pins it deterministically.
        """
        eid = uuid7(_ms=_GOLDEN_MS, _rand=b"\xff" * 10)
        assert eid.bytes[8] == 0xBF, "variant 0b10 over the low six bits of 0xFF"
        assert is_uuid7(eid) is True

    def test_rand_b_tail_is_the_last_seven_random_bytes_in_order(self) -> None:
        """Bytes 9-15 are ``rand[3:10]`` verbatim — no drop, no reorder."""
        eid = uuid7(_ms=_GOLDEN_MS, _rand=_GOLDEN_RAND)
        assert eid.bytes[9:16] == _GOLDEN_RAND[3:10]

    def test_timestamp_occupies_the_first_six_bytes_big_endian(self) -> None:
        eid = uuid7(_ms=_GOLDEN_MS, _rand=_GOLDEN_RAND)
        assert eid.bytes[0:6] == bytes.fromhex("018bcfe56800")


# ===========================================================================
# Entropy: every random byte reaches the output
# ===========================================================================


@pytest.mark.unit
class TestEveryRandomByteContributes:
    def test_rand_a_bytes_change_the_id(self) -> None:
        """Two ids differing ONLY in ``rand[0:2]`` must differ.

        Kills the ``rand_a`` drop even without the layout assertions above.
        """
        tail = bytes([0x00] * 8)
        a = uuid7(_ms=_GOLDEN_MS, _rand=bytes([0x00, 0x00]) + tail)
        b = uuid7(_ms=_GOLDEN_MS, _rand=bytes([0x0F, 0xFF]) + tail)
        assert a != b

    @pytest.mark.parametrize("index", [2, 3, 4, 5, 6, 7, 8, 9])
    def test_each_rand_b_byte_changes_the_id(self, index: int) -> None:
        """Flipping any single byte of ``rand_b`` must change the output.

        Kills mutants that duplicate or skip a byte in the 16-element ``body``
        literal — e.g. ``rand[2]`` written twice, which silently drops
        ``rand[3]``'s entropy while every existing assertion stays green.
        """
        base = bytearray(10)
        flipped = bytearray(10)
        # 0x3F keeps the change inside the six bits that survive the variant
        # mask when index == 2, so the test is meaningful for every index.
        flipped[index] = 0x3F
        assert uuid7(_ms=_GOLDEN_MS, _rand=bytes(base)) != uuid7(
            _ms=_GOLDEN_MS, _rand=bytes(flipped)
        )

    def test_ids_minted_in_the_same_millisecond_still_differ(self) -> None:
        """Random bytes are drawn per call, not once per process."""
        sample = {uuid7(_ms=_GOLDEN_MS) for _ in range(500)}
        assert len(sample) == 500


# ===========================================================================
# _now_ms — a fresh id carries the current wall-clock millisecond
# ===========================================================================


@pytest.mark.unit
class TestFreshIdsCarryTheCurrentTime:
    def test_default_timestamp_is_the_epoch_millisecond(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prefix is exactly ``time_ns() // 1_000_000`` — asserted, not bracketed.

        Kills every rescaling of the divisor: ``// 1_000`` yields microseconds
        (≈1000× too large, and past the 48-bit ceiling), ``// 1_000_000_000``
        yields seconds (≈1000× too small, placing the id in 1970), and reading
        ``monotonic_ns`` instead yields time since boot.

        What is pinned here is the module's *clock source*, not ``_now_ms``
        itself — patching ``_now_ms`` would take the division out of the test
        along with the host. The earlier version of this test instead sampled
        the real wall clock either side of the call and allowed ±5 s, which is
        what made it a scale check rather than a value check, and also what made
        it environmental: a clock correction wider than the window fails a
        correct implementation.

        The pinned nanoseconds carry a 789 000 ns remainder, so truncation and
        rounding disagree — a generator that rounded would produce ...124.
        """
        monkeypatch.setattr(
            event_id_mod,
            "time",
            _FrozenClock(epoch_ns=1_700_000_000_123_789_000, monotonic_ns=4_200_000_000_000),
        )
        embedded = int.from_bytes(uuid7().bytes[:6], "big")
        assert embedded == 1_700_000_000_123

    def test_timestamp_prefix_follows_the_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 48-bit prefix is whatever ``_now_ms`` returned, in that order.

        Driven by a scripted clock rather than the host's. ``uuid7`` reads a
        *wall* clock, and a wall clock can step backwards — NTP correction, a
        DST-adjusted RTC, a VM resuming from a snapshot — so asserting
        monotonicity across two real calls asserts a promise the generator does
        not make, and turns an unrelated host event into a red test. What the
        code does promise is this: the prefix tracks ``_now_ms`` exactly, and
        successive calls preserve the clock's own ordering.
        """
        ticks = iter([1_700_000_000_000, 1_700_000_000_001, 1_700_000_000_500])
        monkeypatch.setattr(event_id_mod, "_now_ms", lambda: next(ticks))
        stamps = [int.from_bytes(uuid7().bytes[:6], "big") for _ in range(3)]
        assert stamps == [1_700_000_000_000, 1_700_000_000_001, 1_700_000_000_500]
        assert stamps == sorted(stamps), "clock order must survive into the prefix"

    def test_a_backwards_clock_still_mints_distinct_valid_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rollback contract, stated rather than assumed.

        This is the case the previous version of the test above got wrong. When
        the clock steps back, ``uuid7`` does NOT keep ids ordered — and it is not
        supposed to. What it must keep is validity and uniqueness, which come
        from the version/variant fields and the 74 random bits, not from the
        timestamp. Pinning that here means a future change to the ordering
        behaviour has to come past a test that says what today's behaviour is.
        """
        ticks = iter([1_700_000_000_000, 1_699_999_999_000])
        monkeypatch.setattr(event_id_mod, "_now_ms", lambda: next(ticks))
        first, second = uuid7(), uuid7()

        assert is_uuid7(first) is True
        assert is_uuid7(second) is True
        assert first != second
        assert int.from_bytes(second.bytes[:6], "big") < int.from_bytes(
            first.bytes[:6], "big"
        ), "a rolled-back clock does produce an earlier prefix — that is the contract"


# ===========================================================================
# Argument guards — the accept side of each boundary
# ===========================================================================


@pytest.mark.unit
class TestTimestampBoundsAcceptTheLegalRange:
    def test_epoch_is_a_legal_timestamp(self) -> None:
        """``_ms=0`` is in range. Kills ``ms < 0`` widened to ``ms <= 0``."""
        eid = uuid7(_ms=0, _rand=_GOLDEN_RAND)
        assert int.from_bytes(eid.bytes[:6], "big") == 0
        assert eid.version == 7

    def test_largest_48_bit_timestamp_is_legal(self) -> None:
        """``(1 << 48) - 1`` is in range. Kills the off-by-one upper guard."""
        ms = (1 << 48) - 1
        eid = uuid7(_ms=ms, _rand=_GOLDEN_RAND)
        assert int.from_bytes(eid.bytes[:6], "big") == ms
        assert eid.version == 7

    def test_one_past_the_ceiling_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="out of UUID v7 range"):
            uuid7(_ms=1 << 48)

    def test_negative_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="out of UUID v7 range"):
            uuid7(_ms=-1)


@pytest.mark.unit
class TestRandLengthIsExact:
    def test_oversize_rand_is_rejected_not_truncated(self) -> None:
        """11 bytes must raise. Kills ``len(rand) != 10`` narrowed to ``< 10``,
        which would silently ignore the extra byte."""
        with pytest.raises(ValueError, match="exactly 10 bytes"):
            uuid7(_rand=b"\x00" * 11)

    def test_undersize_rand_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly 10 bytes"):
            uuid7(_rand=b"\x00" * 9)

    def test_empty_rand_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly 10 bytes"):
            uuid7(_rand=b"")


# ===========================================================================
# is_uuid7 — the version gate is an equality, not a floor
# ===========================================================================


@pytest.mark.unit
class TestIsUuid7VersionGate:
    def test_rejects_version_8(self) -> None:
        """A v8 UUID has a well-formed RFC 4122 variant, so only the version
        equality rejects it. Kills ``!= 7`` relaxed to ``< 7`` or ``in (7, 8)``.
        """
        raw = bytearray(uuid7(_ms=_GOLDEN_MS, _rand=_GOLDEN_RAND).bytes)
        raw[6] = (raw[6] & 0x0F) | 0x80  # version nibble 8, variant untouched
        candidate = uuid.UUID(bytes=bytes(raw))
        assert candidate.version == 8
        assert is_uuid7(candidate) is False
        assert is_uuid7(str(candidate)) is False

    def test_rejects_version_6(self) -> None:
        raw = bytearray(uuid7(_ms=_GOLDEN_MS, _rand=_GOLDEN_RAND).bytes)
        raw[6] = (raw[6] & 0x0F) | 0x60
        candidate = uuid.UUID(bytes=bytes(raw))
        assert candidate.version == 6
        assert is_uuid7(candidate) is False

    def test_rejects_a_version_7_nibble_with_a_non_rfc4122_variant(self) -> None:
        """Layout-level check: a v7 nibble is not enough, the variant must be
        0b10. (Note: CPython's ``UUID.version`` already returns ``None`` for a
        non-RFC-4122 variant, so this pins observable behaviour rather than
        isolating the explicit variant check in ``is_uuid7``.)
        """
        raw = bytearray(uuid7(_ms=_GOLDEN_MS, _rand=_GOLDEN_RAND).bytes)
        raw[8] = 0xC0  # variant bits 0b11
        candidate = uuid.UUID(bytes=bytes(raw))
        assert is_uuid7(candidate) is False
        assert is_uuid7(str(candidate)) is False

    def test_accepts_a_minted_id_in_both_forms(self) -> None:
        eid = uuid7()
        assert is_uuid7(eid) is True
        assert is_uuid7(str(eid)) is True
