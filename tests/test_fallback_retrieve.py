"""Tests for parallax.retrieval.retrievers.fallback_retrieve."""

from __future__ import annotations

import json
import sqlite3

import pytest

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.retrieval.retrievers import fallback_retrieve


def _make_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE claims (
            claim_id TEXT PRIMARY KEY, user_id TEXT, subject TEXT, predicate TEXT,
            object TEXT, source_id TEXT, content_hash TEXT, confidence REAL,
            state TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY, user_id TEXT, actor TEXT, event_type TEXT,
            target_kind TEXT, target_id TEXT, payload_json TEXT, approval_tier TEXT,
            created_at TEXT
        );
        """
    )


def _seed_claims(conn: sqlite3.Connection, n: int, user_id: str = "u1") -> None:
    for i in range(n):
        conn.execute(
            """
            INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"c{i:03d}",
                user_id,
                f"subject_{i}",
                "likes" if i % 2 == 0 else "visited",
                f"object_{i} about tennis and coffee" if i < 20 else f"object_{i}",
                f"s{i}",
                f"h{i}",
                0.8,
                "active",
                f"2026-04-{(i % 28) + 1:02d}T10:00:00Z",
                f"2026-04-{(i % 28) + 1:02d}T10:00:00Z",
            ),
        )
    conn.commit()


def test_fallback_returns_retrieval_evidence():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    _seed_claims(conn, n=50)

    evidence = fallback_retrieve(conn, "u1", "tennis", k_max=32)

    assert isinstance(evidence, RetrievalEvidence)
    assert evidence.diversity_mode in {"mmr_embedding", "mmr_stub_bm25"}
    assert len(evidence.hits) <= 32
    assert len(evidence.hits) >= 1
    # Token budget enforced.
    total = sum(max(1, len(h["text"]) // 4) for h in evidence.hits)
    assert total <= 6000 + 500  # allow one over-the-edge item per spec


def test_empty_pool_demotes_to_fallback():
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    evidence = fallback_retrieve(conn, "u_missing", "anything")
    assert evidence.hits == ()
    assert "demoted_to_fallback" in evidence.notes


def _seed_claims_with_known_dates(
    conn: sqlite3.Connection, n: int, user_id: str = "u1"
) -> list[str]:
    """Seed ``n`` claims with strictly-increasing distinct timestamps.

    Returns the list of created_at strings in chronological order so the test
    can reason about which three are newest without re-sorting the fixture.
    """
    created_ats: list[str] = []
    for i in range(n):
        # Month offset bumps per 300 rows so ordering stays strictly monotonic
        # over the full seed set; second-offset (i*7)%60 keeps every row distinct.
        ts = (
            f"2026-{(i // 300) + 4:02d}-{(i % 28) + 1:02d}"
            f"T{(i % 24):02d}:{(i % 60):02d}:{(i * 7) % 60:02d}Z"
        )
        created_ats.append(ts)
        conn.execute(
            """
            INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"k{i:03d}",
                user_id,
                f"subject_{i}",
                "likes" if i % 2 == 0 else "visited",
                f"object_{i} about tennis and coffee" if i < 20 else f"object_{i}",
                f"s{i}",
                f"h{i}",
                0.8,
                "active",
                ts,
                ts,
            ),
        )
    conn.commit()
    return created_ats


def test_recency_top3_pinned_to_front():
    """Causal assertion: the three pinned items are the genuinely-newest subset.

    The earlier version of this test compared sorted(front) to sorted(all)[:3];
    that is a tautology when the pin logic truncates the front to 3 items. The
    rewrite asserts that the set of three pinned created_at values equals the
    set of the three maximum created_at values across the whole selected pool.
    """
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    created_ats = _seed_claims_with_known_dates(conn, n=50)

    evidence = fallback_retrieve(conn, "u1", "coffee tennis", k_max=10)
    assert len(evidence.hits) >= 3

    front_dates = {h["created_at"] for h in evidence.hits[:3]}
    all_dates = [h["created_at"] for h in evidence.hits]
    # The three pinned at the front are the three largest timestamps in the
    # entire selected hit set — as a *set*, not a sorted-list tautology.
    expected_top3 = set(sorted(all_dates, reverse=True)[:3])
    assert front_dates == expected_top3
    # Every pinned date must be strictly greater than every non-pinned date
    # in the remaining tail — the causal property of "recency pin".
    tail_dates = [h["created_at"] for h in evidence.hits[3:]]
    if tail_dates:
        assert min(front_dates) > max(tail_dates)
    # All three pinned dates should be known-distinct seed values.
    assert front_dates.issubset(set(created_ats))


def test_embedding_cache_reused(monkeypatch):
    """Second fallback_retrieve on same corpus does not re-encode items."""
    from parallax.retrieval import retrievers as rt

    # Reset the module-level caches so this test is independent of order.
    rt._EMB_CACHE.clear()

    conn = sqlite3.connect(":memory:")
    _make_schema(conn)
    _seed_claims(conn, n=30)

    model = rt._load_model()
    if model is None:  # pragma: no cover — embedding SDK missing
        pytest.skip("sentence-transformers unavailable")

    encode_calls = {"n": 0, "sizes": []}
    real_encode = model.encode

    def counting_encode(texts, *args, **kwargs):
        encode_calls["n"] += 1
        encode_calls["sizes"].append(len(texts) if hasattr(texts, "__len__") else 1)
        return real_encode(texts, *args, **kwargs)

    monkeypatch.setattr(model, "encode", counting_encode)

    fallback_retrieve(conn, "u1", "tennis", k_max=16)
    first_total = encode_calls["n"]
    first_sizes = list(encode_calls["sizes"])

    fallback_retrieve(conn, "u1", "tennis", k_max=16)
    second_total = encode_calls["n"]

    # Second call may still call encode for the 1-element query embedding,
    # but must NOT re-encode the item pool.
    large_call_sizes = [s for s in encode_calls["sizes"][first_total:] if s > 1]
    assert not large_call_sizes, (
        f"item pool re-encoded on second call: sizes={encode_calls['sizes']}"
    )
    # At minimum: first call issued two encode calls (query + items); second
    # call issued at most one (query only).
    assert first_total >= 2
    assert second_total - first_total <= 1
    assert any(s > 1 for s in first_sizes), (
        "first call should have encoded the item pool in bulk"
    )


def _seed_events(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str]],
    user_id: str = "u1",
) -> None:
    """Seed the events table.

    ``rows`` is a list of ``(event_id, event_type, payload_json)`` tuples.
    Timestamps are assigned strictly increasing so the candidate ordering is
    deterministic. ``payload_json`` is written verbatim into the TEXT column so
    malformed / non-JSON payloads can be exercised.
    """
    for i, (event_id, event_type, payload_json) in enumerate(rows):
        ts = f"2026-05-{(i % 28) + 1:02d}T08:{i:02d}:00Z"
        conn.execute(
            """
            INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                user_id,
                "system",  # actor
                event_type,
                "claim",  # target_kind
                f"tgt_{i}",  # target_id
                payload_json,
                "auto",  # approval_tier
                ts,
            ),
        )
    conn.commit()


def test_event_branch_payload_text_blob_extraction():
    """Exercise the events branch of ``_fetch_candidates`` (retrievers.py 128-139).

    The base fixture seeds zero events, so the event payload-extraction code is
    otherwise never run. This seeds one event per documented ``payload_json``
    shape and asserts the extracted ``text_blob`` (carried in each hit's
    ``text`` as ``"{event_type}: {text_blob}"``) for every shape:

      1. ``{"text": ...}``    -> the ``text`` value
      2. ``{"content": ...}`` -> the ``content`` value
      3. ``{"summary": ...}`` -> the ``summary`` value
      4. dict with none of those keys -> ``json.dumps(parsed)`` fallback
      5. non-JSON / malformed string -> raw payload (JSONDecodeError except path)

    Deterministic: sentence-transformers is absent in CI, so the bm25-stub path
    selects every candidate when ``k_max`` exceeds the pool size.
    """
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)

    nokeys_payload = '{"kind": "delta", "extra": "no text keys"}'
    # The fallback dumps the *parsed* dict back out with ensure_ascii=False.
    nokeys_expected = json.dumps(json.loads(nokeys_payload), ensure_ascii=False)
    malformed_payload = "raw echo malformed <<< not json"

    # (event_id, event_type, payload_json, expected_text_blob)
    cases = [
        ("ev_text", "evt_text", '{"text": "alpha tennis note"}', "alpha tennis note"),
        ("ev_cont", "evt_content", '{"content": "bravo coffee body"}', "bravo coffee body"),
        ("ev_summ", "evt_summary", '{"summary": "charlie summary line"}', "charlie summary line"),
        ("ev_none", "evt_nokeys", nokeys_payload, nokeys_expected),
        ("ev_raw", "evt_malformed", malformed_payload, malformed_payload),
    ]
    _seed_events(conn, [(eid, etype, pj) for eid, etype, pj, _ in cases])

    # k_max well above the 5-event pool so the bm25 stub keeps every candidate.
    evidence = fallback_retrieve(conn, "u1", "alpha bravo charlie", k_max=32)

    event_hits = [h for h in evidence.hits if h["kind"] == "event"]
    # Every seeded event must come back tagged as an event (the whole pool is
    # events here, so all five survive ranking + token budget).
    assert {h["id"] for h in event_hits} == {eid for eid, _, _, _ in cases}, (
        f"missing event hits: {sorted(h['id'] for h in event_hits)}"
    )

    by_id = {h["id"]: h for h in event_hits}
    for event_id, event_type, _payload, expected_blob in cases:
        hit = by_id[event_id]
        # The event branch composes text as "{event_type}: {text_blob}".
        assert hit["text"] == f"{event_type}: {expected_blob}", (
            f"{event_id}: got {hit['text']!r}, expected text_blob {expected_blob!r}"
        )


def test_event_branch_key_precedence_and_empty_values():
    """Teeth for the ``text or content or summary or dumps`` precedence chain.

    A regression that reorders the keys, drops the ``json.dumps`` fallback, or
    treats empty strings as present would change these blobs and fail here.
    """
    conn = sqlite3.connect(":memory:")
    _make_schema(conn)

    all_three = '{"text": "T", "content": "C", "summary": "S"}'  # text wins
    cont_summ = '{"content": "C", "summary": "S"}'  # content wins
    summ_only = '{"summary": "S"}'  # summary wins
    empty_text = '{"text": "", "content": "real body"}'  # empty falsy -> content
    empty_dict = "{}"  # no keys -> json.dumps("{}") == "{}"

    cases = [
        ("ev_p1", "p1", all_three, "T"),
        ("ev_p2", "p2", cont_summ, "C"),
        ("ev_p3", "p3", summ_only, "S"),
        ("ev_p4", "p4", empty_text, "real body"),
        ("ev_p5", "p5", empty_dict, "{}"),
    ]
    _seed_events(conn, [(eid, etype, pj) for eid, etype, pj, _ in cases])

    evidence = fallback_retrieve(conn, "u1", "anything", k_max=32)
    by_id = {h["id"]: h for h in evidence.hits if h["kind"] == "event"}
    assert set(by_id) == {eid for eid, _, _, _ in cases}

    for event_id, event_type, _payload, expected_blob in cases:
        assert by_id[event_id]["text"] == f"{event_type}: {expected_blob}", (
            f"{event_id}: got {by_id[event_id]['text']!r}, expected {expected_blob!r}"
        )
