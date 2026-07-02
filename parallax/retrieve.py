"""Read-side helpers + v0.3.0 explicit retrieval API for Parallax.

Two surfaces coexist:

* **Legacy, row-level helpers** (``memories_by_user``, ``claims_by_user``,
  ``claims_by_subject``, ``memory_by_content_hash``, ``claim_by_content_hash``) —
  thin wrappers over :func:`parallax.sqlite_store.query` returning plain
  ``dict``. These are used by existing tests and callers; preserved as-is.

* **v0.3.0 explicit retrieval API** — six functions that replace the single
  vague ``search()`` entrypoint that older skills used. Each returns
  ``list[RetrievalHit]`` ordered by score descending, and every hit carries a
  ``project(level: int)`` method implementing 3-layer progressive disclosure
  (L1 title+score, L2 +evidence, L3 full row).

  * :func:`recent_context` — latest events for a session (or most recent session)
  * :func:`by_file` — events that touched a given file path
  * :func:`by_decision` — claim state-changes and decision events
  * :func:`by_bug_fix` — events/claims matching fix/bug tokens
  * :func:`by_timeline` — events in a timestamp window
  * :func:`by_entity` — claims + events referencing a subject

The score signal is deliberately simple (keyword/recency/source-weight mix) —
rich vector scoring is out of scope for v0.3.0.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import sqlite3
from typing import Any

from parallax.obs.metrics import get_counter
from parallax.sqlite_store import query

_c_retrieve = get_counter("retrieve_total")

__all__ = [
    # Legacy helpers
    "memories_by_user",
    "claims_by_user",
    "claims_by_subject",
    "memory_by_content_hash",
    "claim_by_content_hash",
    # v0.3.0
    "RetrievalHit",
    "recent_context",
    "by_file",
    "by_decision",
    "by_bug_fix",
    "by_timeline",
    "by_entity",
    "FILE_EVENT_TYPES",
    # v0.5.0-pre5 trace view
    "RetrievalTrace",
    "RetrievalTraceStage",
    "explain_retrieve",
]



def _to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ----- Legacy helpers -------------------------------------------------------


def memories_by_user(
    conn: sqlite3.Connection, user_id: str, state: str | None = None
) -> list[dict]:
    _c_retrieve.inc()
    if state is None:
        rows = query(conn, "SELECT * FROM memories WHERE user_id = ?", (user_id,))
    else:
        rows = query(
            conn,
            "SELECT * FROM memories WHERE user_id = ? AND state = ?",
            (user_id, state),
        )
    return _to_dicts(rows)


def claims_by_user(
    conn: sqlite3.Connection, user_id: str, state: str | None = None
) -> list[dict]:
    _c_retrieve.inc()
    if state is None:
        rows = query(conn, "SELECT * FROM claims WHERE user_id = ?", (user_id,))
    else:
        rows = query(
            conn,
            "SELECT * FROM claims WHERE user_id = ? AND state = ?",
            (user_id, state),
        )
    return _to_dicts(rows)


def claims_by_subject(
    conn: sqlite3.Connection, user_id: str, subject: str
) -> list[dict]:
    rows = query(
        conn,
        "SELECT * FROM claims WHERE user_id = ? AND subject = ?",
        (user_id, subject),
    )
    return _to_dicts(rows)


def memory_by_content_hash(
    conn: sqlite3.Connection, content_hash: str, *, user_id: str
) -> dict | None:
    """Lookup one memory by ``(content_hash, user_id)``.

    ``user_id`` is keyword-only and required (v0.6.1+). The unique index
    on ``memories(content_hash, user_id)`` permits the same content_hash
    to live under multiple users — looking up by hash alone would risk
    returning a different user's row to the caller. ``content_hash`` for
    memories is currently NOT user-scoped at the hashing layer
    (``sha256(normalize(title || summary || vault_path))``), so the
    user_id filter at lookup time is the security boundary.
    """
    rows = query(
        conn,
        "SELECT * FROM memories WHERE content_hash = ? AND user_id = ? LIMIT 1",
        (content_hash, user_id),
    )
    return dict(rows[0]) if rows else None


def claim_by_content_hash(
    conn: sqlite3.Connection, content_hash: str, *, user_id: str
) -> dict | None:
    """Lookup a claim by ``(content_hash, user_id)``.

    ``user_id`` is keyword-only and required (v0.6.1+). Post-ADR-005
    (v0.5.0-pre1) the claim hash is already user-scoped
    (``sha256(normalize(subject || predicate || object || source_id || user_id))``),
    so a cross-user collision is mathematically negligible. The explicit
    user_id filter is kept anyway as defence-in-depth and to keep the
    memory/claim lookup APIs symmetric. Callers constructing the hash
    externally must pass all five parts; a 4-part hash built with the
    pre-v0.5.0-pre1 formula will silently miss.
    """
    rows = query(
        conn,
        "SELECT * FROM claims WHERE content_hash = ? AND user_id = ? LIMIT 1",
        (content_hash, user_id),
    )
    return dict(rows[0]) if rows else None


# ----- v0.3.0 RetrievalHit --------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RetrievalHit:
    """A single retrieval result with 3-layer progressive disclosure.

    Construct with ``entity_kind`` / ``entity_id`` / ``title`` / ``score`` /
    ``evidence`` (optional L2 one-sentence reason) / ``full`` (optional L3
    dict snapshot of the underlying row) / ``explain`` (mandatory audit
    metadata).

    ``explain`` always carries at minimum ``{'reason': str,
    'score_components': dict[str, float]}`` so score attribution is auditable.
    """

    entity_kind: str
    entity_id: str
    title: str
    score: float
    evidence: str | None
    full: dict | None
    explain: dict

    def project(self, level: int) -> dict:
        """Return the L1/L2/L3 projection. Raises ValueError on bad level."""
        if level not in (1, 2, 3):
            raise ValueError(f"level must be one of 1, 2, 3; got {level!r}")
        base = {
            "entity_kind": self.entity_kind,
            "entity_id": self.entity_id,
            "title": self.title,
            "score": self.score,
        }
        if level == 1:
            return base
        base["evidence"] = self.evidence
        if level == 2:
            return base
        # L3: include full row; fall back to evidence-only when full is None
        base["full"] = self.full if self.full is not None else self.evidence
        return base


# ----- v0.5.0-pre5 trace view ----------------------------------------------


@dataclasses.dataclass(frozen=True)
class RetrievalTraceStage:
    """A single stage of the retrieval funnel, e.g. 'user_scope' → 'filter'.

    ``candidates_in`` and ``candidates_out`` let the operator see where rows
    were dropped (e.g. 50 in → 2 out across the LIKE filter).
    """

    name: str
    candidates_in: int
    candidates_out: int
    detail: str


@dataclasses.dataclass(frozen=True)
class RetrievalTrace:
    """Per-query debug trace — surfaces WHY a retrieval hit (or missed).

    Produced by :func:`explain_retrieve` and by the private ``_TraceBuilder``
    threaded through each v0.3.0 retrieve function when a trace is requested.

    Fields:

    * ``kind`` — retrieval kind (``'recent'``/``'file'``/``'decision'``/
      ``'bug'``/``'entity'``/``'timeline'``).
    * ``params`` — inputs as seen by the dispatcher (user_id, query, limit,
      since, until, …). None values are preserved so the operator can see
      an absent since/until.
    * ``normalized_params`` — transformed inputs — e.g. ``since_norm`` /
      ``until_norm`` for timeline, ``resolved_session_id`` for recent_context.
    * ``sql_fragments`` — primary SQL strings actually executed (omitting
      sqlite_store internals).
    * ``stages`` — funnel stages, recorded in execution order.
    * ``notes`` — free-form diagnostic text (fallback reasons, near-miss
      samples, empty-path skips).
    * ``hits`` — the retrieval result as a frozen tuple so the trace can be
      passed around without accidental mutation.
    """

    kind: str
    params: dict[str, Any]
    normalized_params: dict[str, Any]
    sql_fragments: tuple[str, ...]
    stages: tuple[RetrievalTraceStage, ...]
    notes: tuple[str, ...]
    hits: tuple[RetrievalHit, ...]


class _TraceBuilder:
    """Mutable recorder used internally by each retrieve function.

    Intentionally private: callers interact only with the frozen
    :class:`RetrievalTrace` returned by :meth:`freeze`. When a retrieve
    function receives ``_trace=None`` (the default) it never touches a
    builder, so instrumentation is zero-cost for non-debug paths.
    """

    def __init__(self, *, kind: str, params: dict[str, Any]) -> None:
        self._kind = kind
        self._params = dict(params)
        self._normalized: dict[str, Any] = {}
        self._sql: list[str] = []
        self._stages: list[RetrievalTraceStage] = []
        self._notes: list[str] = []

    def sql(self, fragment: str) -> None:
        self._sql.append(fragment)

    def stage(
        self, name: str, *, candidates_in: int, candidates_out: int, detail: str = ""
    ) -> None:
        self._stages.append(
            RetrievalTraceStage(
                name=name,
                candidates_in=candidates_in,
                candidates_out=candidates_out,
                detail=detail,
            )
        )

    def note(self, text: str) -> None:
        self._notes.append(text)

    def set_normalized(self, d: dict[str, Any]) -> None:
        self._normalized.update(d)

    def freeze(self, *, hits: tuple[RetrievalHit, ...] | list[RetrievalHit]) -> RetrievalTrace:
        return RetrievalTrace(
            kind=self._kind,
            params=dict(self._params),
            normalized_params=dict(self._normalized),
            sql_fragments=tuple(self._sql),
            stages=tuple(self._stages),
            notes=tuple(self._notes),
            hits=tuple(hits),
        )


def _count_events_for_user(conn: sqlite3.Connection, user_id: str) -> int:
    rows = query(conn, "SELECT COUNT(*) AS n FROM events WHERE user_id = ?", (user_id,))
    return int(rows[0]["n"]) if rows else 0


def _count_claims_for_user(conn: sqlite3.Connection, user_id: str) -> int:
    rows = query(conn, "SELECT COUNT(*) AS n FROM claims WHERE user_id = ?", (user_id,))
    return int(rows[0]["n"]) if rows else 0


# ----- v0.3.0 retrieval functions ------------------------------------------


_FIX_TOKENS = ("fix", "bug", "FIX-", "regression", "hotfix")

# Event types that represent file-editing activity. Shared with
# parallax.injector so both surfaces agree on what "a file edit" is.
FILE_EVENT_TYPES = ("tool.edit", "tool.write", "file.edit")

# Used with every LIKE clause that interpolates user-controlled text so that
# literal '%' and '_' in paths/subjects don't silently become wildcards.
_LIKE_ESCAPE = "\\"


def _like_escape(s: str) -> str:
    """Escape LIKE wildcards in user-controlled text.

    Pair with ``ESCAPE '\\'`` in the SQL LIKE clause so that a path like
    ``utils_v2.py`` matches literally instead of treating ``_`` as a wildcard.
    """
    return (
        s.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


# ----- events_fts trigram substring index (migration 0014) ------------------

# The trigram tokenizer indexes 3-character windows, so a MATCH phrase shorter
# than 3 characters has no trigram to look up. Queries below this length fall
# back to the (correct, just unindexed) leading-wildcard LIKE scan so 1-2 char
# paths/subjects keep working.
_TRIGRAM_MIN_CHARS = 3


def _events_fts_available(conn: sqlite3.Connection) -> bool:
    """True when the ``events_fts`` trigram index (migration 0014) is present.

    DBs migrated to <0014, or ``schema.sql``-bootstrapped stores (stress /
    canary harnesses), have ``events`` but no ``events_fts``; those transparently
    fall back to the original LIKE scan.
    """
    rows = query(
        conn,
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'events_fts' LIMIT 1",
    )
    return bool(rows)


def _fts_phrase(text: str) -> str:
    """Wrap user text as a single FTS5 phrase.

    Double-quoting makes the whole string a literal phrase, so trigram
    substring matching mirrors ``LIKE '%text%'`` and FTS5 operator characters
    inside a path/subject (``-``, ``:``, ``*``, ``(`` …) stay literal. Internal
    double quotes are doubled per FTS5 string-literal rules. Pair with a
    ``payload_json MATCH ?`` clause against ``events_fts``.
    """
    return '"' + text.replace('"', '""') + '"'


# STX sentinel bounding the user_tag column value; mirrors ``char(2)`` in
# migration 0014's backfill/trigger. Not a valid character in Parallax user_ids.
_FTS_USER_SENTINEL = "\x02"


def _fts_user_tag(user_id: str) -> str:
    """Sentinel-bounded encoding of ``user_id`` for the ``events_fts.user_tag``
    column.

    Must stay byte-identical to migration 0014's ``char(2) || user_id ||
    char(2)`` so a query-side tag matches the stored tag. The bounds keep the
    tag ``>= 3`` chars (so a 1-char ``user_id`` still yields a trigram) and stop
    one id matching as a substring of another under trigram search.
    """
    return f"{_FTS_USER_SENTINEL}{user_id}{_FTS_USER_SENTINEL}"


def _fts_scoped_match(user_id: str, payload_expr: str) -> str:
    """Build a user-scoped FTS5 MATCH expression.

    ``payload_expr`` is the already-quoted payload phrase (or parenthesised OR
    of phrases). Restricting the term to the ``payload_json`` column keeps it
    from matching against the ``user_tag`` column, and the ``user_tag`` filter
    confines the whole match to one tenant's rows inside the index — rather than
    matching a common term across every user and discarding the rest after the
    primary-key join.
    """
    return (
        f"user_tag:{_fts_phrase(_fts_user_tag(user_id))} "
        f"AND payload_json:{payload_expr}"
    )


def _parse_iso(ts: str) -> _dt.datetime:
    """Parse ISO-8601; tolerant of trailing 'Z'."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(ts)


def _iso_normalize(ts: str, *, kind: str) -> str:
    """Normalize an ISO-8601 string to the exact form used by ``now_iso()``.

    The output always carries both a microsecond component and a ``+00:00``
    offset so lexicographic comparison against stored ``created_at`` values
    is stable. Required by the SQLite ``created_at >= ? AND created_at <= ?``
    window in :func:`by_timeline`:

    * ``kind='since'`` — micro=0 is preserved; bound is inclusive from the
      start of the second.
    * ``kind='until'`` — if the input microsecond is 0, it is expanded to
      ``999999`` so the second-boundary is inclusive to the end of the
      second. Without this, ``now_iso()`` rows like
      ``"...T12:00:00.500000+00:00"`` fall OUTSIDE a query whose
      ``until="...T12:00:00Z"`` normalized naively (BUG 1).

    ``kind`` is keyword-only and has no default: a silent miscall such as
    ``_iso_normalize(until_str)`` would apply ``since`` semantics to an
    ``until`` bound and drop boundary events. Requiring an explicit kind
    makes that misuse impossible.
    """
    if kind not in ("since", "until"):
        raise ValueError(f"kind must be 'since' or 'until'; got {kind!r}")
    dt = _parse_iso(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    dt = dt.astimezone(_dt.UTC)
    if kind == "until" and dt.microsecond == 0:
        dt = dt.replace(microsecond=999_999)
    # Force the ``.SSSSSS`` component so the lex-compared bound always has
    # the same layout as stored ``now_iso()`` rows (BUG 1/4).
    return dt.isoformat(timespec="microseconds")


def _recency_score(created_at: str, now: _dt.datetime | None = None) -> float:
    """Return a [0,1] recency weight; newer → higher."""
    try:
        t = _parse_iso(created_at)
    except Exception:
        return 0.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.UTC)
    ref = now or _dt.datetime.now(_dt.UTC)
    age_h = max((ref - t).total_seconds() / 3600.0, 0.0)
    # half-life ~ 24h
    return max(0.0, min(1.0, 1.0 / (1.0 + age_h / 24.0)))


def _event_title(row: dict) -> str:
    """Compact human-readable title for an event row."""
    etype = row.get("event_type", "event")
    tkind = row.get("target_kind")
    tid = row.get("target_id")
    if tkind and tid:
        return f"{etype} [{tkind}:{tid}]"
    return str(etype)


def _event_to_hit(row: dict, *, reason: str, score_components: dict[str, float]) -> RetrievalHit:
    score = round(sum(score_components.values()), 6)
    return RetrievalHit(
        entity_kind="event",
        entity_id=str(row.get("event_id", "")),
        title=_event_title(row),
        score=score,
        evidence=f"{row.get('created_at','?')} {row.get('event_type','?')} "
                 f"{row.get('payload_json','')[:160]}",
        full=row,
        explain={"reason": reason, "score_components": score_components},
    )


def _claim_to_hit(row: dict, *, reason: str, score_components: dict[str, float]) -> RetrievalHit:
    score = round(sum(score_components.values()), 6)
    title = f"{row.get('subject','')} {row.get('predicate','')} {row.get('object','')}"
    return RetrievalHit(
        entity_kind="claim",
        entity_id=str(row.get("claim_id", "")),
        title=title.strip() or "(empty claim)",
        score=score,
        evidence=f"confidence={row.get('confidence')} state={row.get('state')}",
        full=row,
        explain={"reason": reason, "score_components": score_components},
    )


def recent_context(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    session_id: str | None = None,
    limit: int = 20,
    _trace: _TraceBuilder | None = None,
) -> list[RetrievalHit]:
    """Most recent events for a session.

    When ``session_id`` is None, the scan is scoped to the latest session (the
    session_id of the newest ``session.start`` row, if any; otherwise the
    newest event regardless of session). Score = recency (1/(1+age_h/24)).

    ``_trace`` is a private debug hook used by :func:`explain_retrieve`; when
    None, the function is byte-identical to its pre-v0.5.0-pre5 behaviour.
    """
    explicit_session = session_id is not None
    if session_id is None:
        row = query(
            conn,
            "SELECT session_id FROM events WHERE user_id = ? AND event_type = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, "session.start"),
        )
        session_id = row[0]["session_id"] if row else None

    fell_back = session_id is None and not explicit_session
    if session_id is None:
        sql = (
            "SELECT * FROM events WHERE user_id = ? ORDER BY created_at DESC LIMIT ?"
        )
        rows = query(conn, sql, (user_id, limit))
        reason_tmpl = "recent_context fallback (no session.start found); newest events for user"
    else:
        sql = (
            "SELECT * FROM events WHERE user_id = ? AND session_id = ? "
            "ORDER BY created_at DESC LIMIT ?"
        )
        rows = query(conn, sql, (user_id, session_id, limit))
        reason_tmpl = f"recent_context match on session_id={session_id!r}"

    hits: list[RetrievalHit] = []
    for r in _to_dicts(rows):
        hits.append(
            _event_to_hit(
                r,
                reason=reason_tmpl,
                score_components={"recency": _recency_score(r.get("created_at", ""))},
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)

    if _trace is not None:
        total = _count_events_for_user(conn, user_id)
        _trace.set_normalized({"resolved_session_id": session_id})
        _trace.sql(sql)
        _trace.stage(
            "user_scope",
            candidates_in=total,
            candidates_out=total,
            detail=f"events rows for user_id={user_id!r}",
        )
        _trace.stage(
            "session_scope",
            candidates_in=total,
            candidates_out=len(hits),
            detail=f"session_id={session_id!r} limit={limit}",
        )
        _trace.stage(
            "final", candidates_in=len(hits), candidates_out=len(hits), detail=""
        )
        if fell_back:
            _trace.note(
                "no session.start event found — fell back to newest events for user"
            )
    return hits


def by_file(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    path: str,
    limit: int = 20,
    _trace: _TraceBuilder | None = None,
) -> list[RetrievalHit]:
    """Events whose payload references a given file path.

    v0.3.0 uses a LIKE scan on the JSON payload for simplicity; later
    versions can swap to a structured path index without changing callers.
    ``%`` / ``_`` in the path are escaped so file names like
    ``utils_v2.py`` match literally.
    """
    if not path:
        if _trace is not None:
            _trace.note("empty path — skipped by_file scan")
            _trace.stage(
                "input_guard",
                candidates_in=0,
                candidates_out=0,
                detail="path is empty",
            )
        return []
    placeholders = ",".join("?" * len(FILE_EVENT_TYPES))
    use_fts = len(path) >= _TRIGRAM_MIN_CHARS and _events_fts_available(conn)
    if use_fts:
        pattern = _fts_scoped_match(user_id, _fts_phrase(path))
        sql = (
            f"SELECT e.* FROM events e "
            f"JOIN events_fts f ON f.event_id = e.event_id "
            f"WHERE e.user_id = ? AND e.event_type IN ({placeholders}) "
            f"AND f.events_fts MATCH ? "
            f"ORDER BY e.created_at DESC LIMIT ?"
        )
        filter_detail = f"events_fts MATCH {pattern!r} (user-scoped trigram)"
    else:
        pattern = f"%{_like_escape(path)}%"
        sql = (
            f"SELECT * FROM events WHERE user_id = ? AND event_type IN ({placeholders}) "
            "AND payload_json LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT ?"
        )
        filter_detail = f"payload_json LIKE {pattern!r}"
    rows = query(conn, sql, (user_id, *FILE_EVENT_TYPES, pattern, limit))
    hits: list[RetrievalHit] = []
    for r in _to_dicts(rows):
        hits.append(
            _event_to_hit(
                r,
                reason=f"by_file match on events.payload_json LIKE %{path}%",
                score_components={
                    "keyword": 0.6,
                    "recency": 0.3 * _recency_score(r.get("created_at", "")),
                    "source": 0.1,
                },
            )
        )

    if _trace is not None:
        total = _count_events_for_user(conn, user_id)
        # Split the filter into event-type and payload-LIKE stages so the
        # operator can tell whether the corpus had zero file events at all
        # (type filter dropped everything) or had file events but none
        # referenced the requested path (payload filter dropped everything).
        type_rows = query(
            conn,
            f"SELECT COUNT(*) AS n FROM events WHERE user_id = ? "
            f"AND event_type IN ({placeholders})",
            (user_id, *FILE_EVENT_TYPES),
        )
        type_count = int(type_rows[0]["n"]) if type_rows else 0
        _trace.set_normalized(
            {"payload_pattern": pattern, "event_types": list(FILE_EVENT_TYPES)}
        )
        _trace.sql(sql)
        _trace.stage(
            "user_scope",
            candidates_in=total,
            candidates_out=total,
            detail=f"events rows for user_id={user_id!r}",
        )
        _trace.stage(
            "event_type_filter",
            candidates_in=total,
            candidates_out=type_count,
            detail=f"event_type IN {FILE_EVENT_TYPES}",
        )
        _trace.stage(
            "payload_filter",
            candidates_in=type_count,
            candidates_out=len(hits),
            detail=filter_detail,
        )
        _trace.stage(
            "final", candidates_in=len(hits), candidates_out=len(hits), detail=""
        )
    return hits


def by_decision(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    limit: int = 20,
    _trace: _TraceBuilder | None = None,
) -> list[RetrievalHit]:
    """Claim state-changes + decision-family events.

    When the event targets a ``claim`` row, the hit title is enriched with
    the claim's ``subject predicate object`` so injected reminders read as
    "P is ok" rather than the opaque event id.
    """
    sql = (
        "SELECT * FROM events WHERE user_id = ? "
        "AND (event_type = 'claim.state_changed' OR event_type LIKE 'decision.%') "
        "ORDER BY created_at DESC LIMIT ?"
    )
    rows = query(conn, sql, (user_id, limit))
    dicts = _to_dicts(rows)
    # Batch-fetch all referenced claims in one query to avoid N+1.
    claim_ids = [
        r["target_id"]
        for r in dicts
        if r.get("target_kind") == "claim" and r.get("target_id")
    ]
    spo_by_id: dict[str, str] = {}
    if claim_ids:
        placeholders = ",".join("?" * len(claim_ids))
        claim_rows = query(
            conn,
            f"SELECT claim_id, subject, predicate, object FROM claims "
            f"WHERE claim_id IN ({placeholders})",
            tuple(claim_ids),
        )
        for c in claim_rows:
            spo = f"{c['subject']} {c['predicate']} {c['object']}".strip()
            if spo:
                spo_by_id[c["claim_id"]] = spo

    hits: list[RetrievalHit] = []
    for r in dicts:
        hit = _event_to_hit(
            r,
            reason="by_decision match on event_type claim.state_changed OR decision.*",
            score_components={
                "keyword": 0.5,
                "recency": 0.5 * _recency_score(r.get("created_at", "")),
            },
        )
        spo = spo_by_id.get(r.get("target_id") or "")
        if spo:
            hit = dataclasses.replace(hit, title=f"{spo} — {hit.title}")
        hits.append(hit)

    if _trace is not None:
        total = _count_events_for_user(conn, user_id)
        _trace.sql(sql)
        _trace.stage(
            "user_scope",
            candidates_in=total,
            candidates_out=total,
            detail=f"events rows for user_id={user_id!r}",
        )
        _trace.stage(
            "decision_filter",
            candidates_in=total,
            candidates_out=len(hits),
            detail="event_type = 'claim.state_changed' OR LIKE 'decision.%'",
        )
        _trace.stage(
            "final", candidates_in=len(hits), candidates_out=len(hits), detail=""
        )
    return hits


def by_bug_fix(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    limit: int = 20,
    _trace: _TraceBuilder | None = None,
) -> list[RetrievalHit]:
    """Events + claims whose text matches fix/bug tokens."""
    hits: list[RetrievalHit] = []

    # Every fix-token is a fixed >=3-char constant, so the trigram index can
    # serve the whole OR. If the index is missing, or a future token drops
    # below the trigram floor, fall back to the leading-wildcard LIKE OR.
    use_fts = _events_fts_available(conn) and all(
        len(tok) >= _TRIGRAM_MIN_CHARS for tok in _FIX_TOKENS
    )
    if use_fts:
        payload_or = " OR ".join(_fts_phrase(tok) for tok in _FIX_TOKENS)
        match_expr = _fts_scoped_match(user_id, f"({payload_or})")
        event_sql = (
            "SELECT e.* FROM events e "
            "JOIN events_fts f ON f.event_id = e.event_id "
            "WHERE e.user_id = ? AND f.events_fts MATCH ? "
            "ORDER BY e.created_at DESC LIMIT ?"
        )
        erows = query(conn, event_sql, (user_id, match_expr, limit))
    else:
        like_clauses = " OR ".join(["payload_json LIKE ?"] * len(_FIX_TOKENS))
        like_params = tuple(f"%{tok}%" for tok in _FIX_TOKENS)
        event_sql = (
            f"SELECT * FROM events WHERE user_id = ? AND ({like_clauses}) "
            "ORDER BY created_at DESC LIMIT ?"
        )
        erows = query(conn, event_sql, (user_id, *like_params, limit))
    for r in _to_dicts(erows):
        hits.append(
            _event_to_hit(
                r,
                reason="by_bug_fix match on events.payload_json LIKE fix/bug tokens",
                score_components={
                    "keyword": 0.6,
                    "recency": 0.4 * _recency_score(r.get("created_at", "")),
                },
            )
        )

    # One query with every (subject|predicate|object) x token LIKE OR'd together,
    # instead of len(_FIX_TOKENS) round-trips each returning up to `limit` rows.
    claim_or = " OR ".join(
        ["subject LIKE ?", "predicate LIKE ?", "object LIKE ?"] * len(_FIX_TOKENS)
    )
    claim_params: list[str] = []
    for tok in _FIX_TOKENS:
        like = f"%{tok}%"
        claim_params.extend([like, like, like])
    # ORDER BY is mandatory — without it SQLite returns heap-order rows and
    # LIMIT ? silently drops high-confidence claims at rowid > limit (BUG 2,
    # v0.5.0-pre1). claim_id ASC is the deterministic tiebreak.
    claim_sql = (
        f"SELECT * FROM claims WHERE user_id = ? AND ({claim_or}) "
        "ORDER BY confidence DESC, updated_at DESC, claim_id ASC LIMIT ?"
    )
    claim_rows = query(conn, claim_sql, (user_id, *claim_params, limit))
    seen: set[str] = set()
    for r in _to_dicts(claim_rows):
        cid = r.get("claim_id", "")
        if cid in seen:
            continue
        seen.add(cid)
        hits.append(
            _claim_to_hit(
                r,
                reason="by_bug_fix match on claims.(subject|predicate|object) LIKE fix/bug",
                score_components={"keyword": 0.7, "source": 0.1},
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:limit]

    if _trace is not None:
        events_total = _count_events_for_user(conn, user_id)
        claims_total = _count_claims_for_user(conn, user_id)
        event_matches = len(erows)
        claim_matches = len(claim_rows)
        _trace.set_normalized({"fix_tokens": list(_FIX_TOKENS)})
        _trace.sql(event_sql)
        _trace.sql(claim_sql)
        _trace.stage(
            "user_scope_events",
            candidates_in=events_total,
            candidates_out=events_total,
            detail=f"events rows for user_id={user_id!r}",
        )
        _trace.stage(
            "payload_token_filter",
            candidates_in=events_total,
            candidates_out=event_matches,
            detail=f"events.payload_json LIKE %<tok>% for tok in {list(_FIX_TOKENS)}",
        )
        _trace.stage(
            "user_scope_claims",
            candidates_in=claims_total,
            candidates_out=claims_total,
            detail=f"claims rows for user_id={user_id!r}",
        )
        _trace.stage(
            "spo_token_filter",
            candidates_in=claims_total,
            candidates_out=claim_matches,
            detail=f"claims.(subject|predicate|object) LIKE %<tok>% for tok in {list(_FIX_TOKENS)}",
        )
        _trace.stage(
            "final",
            candidates_in=event_matches + claim_matches,
            candidates_out=len(hits),
            detail=f"merge + sort by score DESC; limit={limit}",
        )
    return hits


def by_timeline(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    since: str,
    until: str,
    limit: int = 50,
    _trace: _TraceBuilder | None = None,
) -> list[RetrievalHit]:
    """Events in a timestamp window [since, until] ordered ascending."""
    try:
        since_norm = _iso_normalize(since, kind="since")
        until_norm = _iso_normalize(until, kind="until")
    except ValueError as exc:
        raise ValueError(f"by_timeline: could not parse since/until ISO-8601 ({exc})") from exc
    if since_norm > until_norm:
        raise ValueError(
            f"by_timeline: since ({since}) must be <= until ({until})"
        )
    sql = (
        "SELECT * FROM events WHERE user_id = ? AND created_at >= ? AND created_at <= ? "
        "ORDER BY created_at ASC LIMIT ?"
    )
    rows = query(conn, sql, (user_id, since_norm, until_norm, limit))
    hits: list[RetrievalHit] = []
    for r in _to_dicts(rows):
        hits.append(
            _event_to_hit(
                r,
                reason=f"by_timeline match on created_at BETWEEN {since!r} AND {until!r}",
                score_components={"recency": _recency_score(r.get("created_at", ""))},
            )
        )

    if _trace is not None:
        total = _count_events_for_user(conn, user_id)
        _trace.set_normalized({"since_norm": since_norm, "until_norm": until_norm})
        _trace.sql(sql)
        _trace.stage(
            "user_scope",
            candidates_in=total,
            candidates_out=total,
            detail=f"events rows for user_id={user_id!r}",
        )
        _trace.stage(
            "time_window",
            candidates_in=total,
            candidates_out=len(hits),
            detail=f"created_at >= {since_norm!r} AND created_at <= {until_norm!r}",
        )
        _trace.stage(
            "final", candidates_in=len(hits), candidates_out=len(hits), detail=""
        )
    return hits


def by_entity(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    subject: str,
    limit: int = 20,
    _trace: _TraceBuilder | None = None,
) -> list[RetrievalHit]:
    """Claims whose subject matches (exact + case-insensitive prefix) + events
    whose payload references the subject."""
    if not subject:
        if _trace is not None:
            _trace.note("empty subject — skipped by_entity scan")
            _trace.stage(
                "input_guard",
                candidates_in=0,
                candidates_out=0,
                detail="subject is empty",
            )
        return []
    hits: list[RetrievalHit] = []

    prefix_like = f"{_like_escape(subject.lower())}%"
    # ORDER BY is mandatory — see by_bug_fix claim SELECT above (BUG 2).
    claim_sql = (
        "SELECT * FROM claims WHERE user_id = ? AND "
        "(subject = ? OR LOWER(subject) LIKE ? ESCAPE '\\') "
        "ORDER BY confidence DESC, updated_at DESC, claim_id ASC LIMIT ?"
    )
    claim_rows = query(conn, claim_sql, (user_id, subject, prefix_like, limit))
    for r in _to_dicts(claim_rows):
        exact = r.get("subject") == subject
        hits.append(
            _claim_to_hit(
                r,
                reason=f"by_entity match on claims.subject "
                       f"{'exact' if exact else 'prefix'} {subject!r}",
                score_components={
                    "keyword": 1.0 if exact else 0.6,
                    "source": 0.1,
                },
            )
        )

    use_fts = len(subject) >= _TRIGRAM_MIN_CHARS and _events_fts_available(conn)
    if use_fts:
        event_pattern = _fts_scoped_match(user_id, _fts_phrase(subject))
        event_sql = (
            "SELECT e.* FROM events e "
            "JOIN events_fts f ON f.event_id = e.event_id "
            "WHERE e.user_id = ? AND f.events_fts MATCH ? "
            "ORDER BY e.created_at DESC LIMIT ?"
        )
        event_filter_detail = f"events_fts MATCH {event_pattern!r} (user-scoped trigram)"
    else:
        event_pattern = f"%{_like_escape(subject)}%"
        event_sql = (
            "SELECT * FROM events WHERE user_id = ? AND payload_json LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?"
        )
        event_filter_detail = f"events.payload_json LIKE {event_pattern!r}"
    event_rows = query(conn, event_sql, (user_id, event_pattern, limit))
    for r in _to_dicts(event_rows):
        hits.append(
            _event_to_hit(
                r,
                reason=f"by_entity match on events.payload_json LIKE %{subject}%",
                score_components={
                    "keyword": 0.5,
                    "recency": 0.3 * _recency_score(r.get("created_at", "")),
                },
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    hits = hits[:limit]

    if _trace is not None:
        events_total = _count_events_for_user(conn, user_id)
        claims_total = _count_claims_for_user(conn, user_id)
        claim_matches = len(claim_rows)
        event_matches = len(event_rows)
        _trace.set_normalized(
            {"subject_lower": subject.lower(), "prefix_like": prefix_like}
        )
        _trace.sql(claim_sql)
        _trace.sql(event_sql)
        _trace.stage(
            "user_scope_claims",
            candidates_in=claims_total,
            candidates_out=claims_total,
            detail=f"claims rows for user_id={user_id!r}",
        )
        _trace.stage(
            "subject_filter",
            candidates_in=claims_total,
            candidates_out=claim_matches,
            detail=f"subject = {subject!r} OR LOWER(subject) LIKE {prefix_like!r}",
        )
        _trace.stage(
            "user_scope_events",
            candidates_in=events_total,
            candidates_out=events_total,
            detail=f"events rows for user_id={user_id!r}",
        )
        _trace.stage(
            "payload_filter",
            candidates_in=events_total,
            candidates_out=event_matches,
            detail=event_filter_detail,
        )
        _trace.stage(
            "final",
            candidates_in=claim_matches + event_matches,
            candidates_out=len(hits),
            detail=f"merge + sort by score DESC; limit={limit}",
        )
    return hits


# ----- v0.5.0-pre5 explain_retrieve dispatcher -----------------------------


_RETRIEVE_KINDS = ("recent", "file", "decision", "bug", "entity", "timeline")


_RECENT_CLAIMS_SQL = (
    "SELECT claim_id, subject, predicate, object FROM claims "
    "WHERE user_id = ? ORDER BY updated_at DESC, claim_id ASC LIMIT 3"
)


def _fmt_claim_note(kind: str, label: str, row: Any) -> str:
    """Format one near-miss note line for a claim row."""
    spo = f"{row['subject']} {row['predicate']} {row['object']}".strip()
    return f"near_miss({kind}) {label}: sample={row['claim_id']}: {spo}"


def _fmt_file_note(label: str, row: Any) -> str:
    """Format one near-miss note line for a file-event row.

    Payload is truncated to 80 chars and newlines flattened so each note
    occupies a single line in the CLI trace output.
    """
    payload = (row["payload_json"] or "")[:80].replace("\n", " ")
    return f"near_miss(file) {label}: sample={row['event_id']}: {payload}"


def _near_miss_notes(
    conn: sqlite3.Connection, *, kind: str, user_id: str, query_text: str = ""
) -> list[str]:
    """Return up to 3 diagnostic sample-row notes when a retrieval missed.

    The goal is to let an operator distinguish the two common miss modes —
    "filter too strict" vs. "DB is empty for this user" vs. "DB has rows but
    none relate to the query" — without opening sqlite3 by hand. Called only
    when ``hits`` is empty.

    For ``kind='entity'`` / ``kind='file'`` we first try a permissive LIKE
    fuzzy match against ``query_text`` (case-insensitive, substring). If that
    yields rows, they are labelled ``near_miss(<kind>) fuzzy``. Only when the
    fuzzy pass also returns nothing do we fall back to the 3 most-recent rows
    labelled ``near_miss(<kind>) recent`` — so "sample" always means "the DB
    has these rows but they didn't match", never silently masquerades as
    "nearby matches".
    """
    notes: list[str] = []
    if kind == "entity":
        if _count_claims_for_user(conn, user_id) == 0:
            return [f"near_miss(entity): corpus empty for user_id={user_id!r}"]
        if query_text:
            fuzzy = f"%{_like_escape(query_text.lower())}%"
            rows = query(
                conn,
                "SELECT claim_id, subject, predicate, object FROM claims "
                "WHERE user_id = ? AND (LOWER(subject) LIKE ? ESCAPE '\\' "
                "OR LOWER(object) LIKE ? ESCAPE '\\') "
                "ORDER BY updated_at DESC, claim_id ASC LIMIT 3",
                (user_id, fuzzy, fuzzy),
            )
            notes = [_fmt_claim_note("entity", "fuzzy", r) for r in rows]
            if notes:
                return notes
        rows = query(conn, _RECENT_CLAIMS_SQL, (user_id,))
        return [_fmt_claim_note("entity", "recent", r) for r in rows]

    if kind == "file":
        placeholders = ",".join("?" * len(FILE_EVENT_TYPES))
        base = (
            f"SELECT event_id, payload_json FROM events WHERE user_id = ? "
            f"AND event_type IN ({placeholders}) "
        )
        if query_text:
            fuzzy = f"%{_like_escape(query_text.lower())}%"
            rows = query(
                conn,
                base + "AND LOWER(payload_json) LIKE ? ESCAPE '\\' "
                "ORDER BY created_at DESC LIMIT 3",
                (user_id, *FILE_EVENT_TYPES, fuzzy),
            )
            notes = [_fmt_file_note("fuzzy", r) for r in rows]
            if notes:
                return notes
        rows = query(
            conn,
            base + "ORDER BY created_at DESC LIMIT 3",
            (user_id, *FILE_EVENT_TYPES),
        )
        if not rows:
            return [f"near_miss(file): corpus empty for user_id={user_id!r}"]
        return [_fmt_file_note("recent", r) for r in rows]

    if kind == "bug":
        if _count_claims_for_user(conn, user_id) == 0:
            return [f"near_miss(bug): corpus empty for user_id={user_id!r}"]
        rows = query(conn, _RECENT_CLAIMS_SQL, (user_id,))
        return [_fmt_claim_note("bug", "recent", r) for r in rows]

    return notes


def explain_retrieve(
    conn: sqlite3.Connection,
    *,
    kind: str,
    user_id: str,
    query_text: str = "",
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> RetrievalTrace:
    """Dispatch to the v0.3.0 retrieval API and return a populated trace.

    ``kind`` ∈ {'recent','file','decision','bug','entity','timeline'}.

    * ``query_text`` feeds ``path`` for ``file`` and ``subject`` for
      ``entity``. Renamed from ``query`` to avoid shadowing the module-level
      ``query`` helper imported from :mod:`parallax.sqlite_store`.
    * ``since`` / ``until`` are required for ``timeline`` and ignored for
      other kinds (they're still echoed into ``params``).
    * For ``kind`` ∈ {'entity','file','bug'}, when the call produces zero
      hits a ``near_miss`` note is attached — for ``entity`` / ``file`` this
      is a permissive LIKE fuzzy match against the query text; only if that
      also yields nothing do we fall back to the 3 most-recent rows (so the
      operator can still distinguish "empty corpus" from "non-matching
      corpus").
    """
    if kind not in _RETRIEVE_KINDS:
        raise ValueError(
            f"explain_retrieve: unknown kind {kind!r}; expected one of {list(_RETRIEVE_KINDS)}"
        )

    params: dict[str, Any] = {
        "kind": kind,
        "user_id": user_id,
        "query": query_text,
        "limit": limit,
        "since": since,
        "until": until,
    }
    builder = _TraceBuilder(kind=kind, params=params)

    if kind == "recent":
        hits = recent_context(conn, user_id=user_id, limit=limit, _trace=builder)
    elif kind == "file":
        hits = by_file(
            conn, user_id=user_id, path=query_text, limit=limit, _trace=builder
        )
    elif kind == "decision":
        hits = by_decision(conn, user_id=user_id, limit=limit, _trace=builder)
    elif kind == "bug":
        hits = by_bug_fix(conn, user_id=user_id, limit=limit, _trace=builder)
    elif kind == "entity":
        hits = by_entity(
            conn, user_id=user_id, subject=query_text, limit=limit, _trace=builder
        )
    else:  # timeline
        if since is None or until is None:
            raise ValueError(
                "explain_retrieve(kind='timeline'): since and until are required"
            )
        hits = by_timeline(
            conn,
            user_id=user_id,
            since=since,
            until=until,
            limit=limit,
            _trace=builder,
        )

    if not hits and kind in ("entity", "file", "bug"):
        for note in _near_miss_notes(
            conn, kind=kind, user_id=user_id, query_text=query_text
        ):
            builder.note(note)

    return builder.freeze(hits=hits)
