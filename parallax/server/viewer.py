"""Read-only web viewer for Parallax kernel data.

Provides a single-page HTML debug interface at ``/viewer/`` and three JSON
data endpoints (events, claims, retrieve-explain). All routes are gated
behind :func:`parallax.server.auth.require_auth`.

The router is only registered when ``PARALLAX_VIEWER_ENABLED=1`` — see
:func:`parallax.server.app.create_app` for the conditional include.
"""
# ruff: noqa: E501
# (The _HTML template embeds CSS/JS that cannot be line-wrapped without
# breaking layout. Long-line ban is disabled file-wide.)

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from parallax.retrieve import RetrievalTrace, claims_by_user, explain_retrieve
from parallax.server.auth import current_user_id, require_auth
from parallax.server.deps import get_conn

__all__ = ["router"]

_CONN_DEP = Depends(get_conn)

router = APIRouter(
    prefix="/viewer",
    tags=["viewer"],
    dependencies=[Depends(require_auth)],
)

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Parallax Viewer</title>
<style>
body{font-family:monospace;margin:1rem 2rem;background:#111;color:#eee}
h1{color:#7df}
nav button{margin:0 4px;padding:4px 12px;cursor:pointer;background:#333;color:#eee;border:1px solid #555;border-radius:3px}
nav button.active{background:#7df;color:#111}
.tab{display:none}.tab.active{display:block}
table{border-collapse:collapse;width:100%;font-size:.85rem;margin-top:.5rem}
th{background:#222;text-align:left;padding:4px 8px;border-bottom:2px solid #555}
td{padding:3px 8px;border-bottom:1px solid #333;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
tr:hover td{background:#1a1a2e}
.ctrl{margin:.5rem 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.ctrl input,.ctrl select{background:#222;color:#eee;border:1px solid #555;padding:3px 6px;border-radius:3px}
.ctrl button{padding:3px 10px;cursor:pointer;background:#333;color:#eee;border:1px solid #555;border-radius:3px}
#err{color:#f66;margin:.4rem 0}
pre{background:#1a1a1a;padding:.5rem;overflow:auto;max-height:400px;border:1px solid #444}
</style>
</head>
<body>
<h1>parallax viewer</h1>
<nav>
  <button class="active" onclick="show('events',this)">events</button>
  <button onclick="show('claims',this)">claims</button>
  <button onclick="show('retrieve',this)">retrieve-explain</button>
</nav>
<div id="err"></div>

<!-- events tab -->
<div id="events" class="tab active">
  <div class="ctrl">
    user_id: <input id="ev-user" value="" placeholder="all">
    limit: <input id="ev-limit" value="100" size="5">
    <button onclick="loadEvents()">load</button>
  </div>
  <table id="ev-table">
    <thead><tr><th>event_id</th><th>kind</th><th>target_kind</th><th>target_id</th><th>payload</th><th>created_at</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<!-- claims tab -->
<div id="claims" class="tab">
  <div class="ctrl">
    user_id: <input id="cl-user" value="" placeholder="required">
    limit: <input id="cl-limit" value="100" size="5">
    <button onclick="loadClaims()">load</button>
  </div>
  <table id="cl-table">
    <thead><tr><th>claim_id</th><th>subject</th><th>predicate</th><th>object</th><th>confidence</th><th>state</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<!-- retrieve tab -->
<div id="retrieve" class="tab">
  <div class="ctrl">
    user_id: <input id="rt-user" value="" placeholder="required">
    query: <input id="rt-q" value="">
    kind: <select id="rt-kind">
      <option>by_entity</option><option>recent</option><option>file</option>
      <option>decision</option><option>bug</option><option>timeline</option>
    </select>
    <button onclick="loadRetrieve()">explain</button>
  </div>
  <pre id="rt-out">(click explain)</pre>
</div>

<script>
function show(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(name).classList.add('active');
  btn.classList.add('active');
}
function err(msg) { document.getElementById('err').textContent = msg || ''; }

async function apiFetch(url) {
  err('');
  const resp = await fetch(url);
  if (!resp.ok) { err('HTTP ' + resp.status + ': ' + await resp.text()); return null; }
  return resp.json();
}

function populateTable(tableId, rows, cols) {
  const tb = document.querySelector('#' + tableId + ' tbody');
  tb.innerHTML = '';
  (rows || []).forEach(r => {
    const tr = document.createElement('tr');
    cols.forEach(c => {
      const td = document.createElement('td');
      const v = r[c];
      td.textContent = v == null ? '' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
      td.title = td.textContent;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
}

async function loadEvents() {
  const uid = document.getElementById('ev-user').value.trim();
  const lim = document.getElementById('ev-limit').value.trim() || '100';
  let url = '/viewer/events.json?limit=' + encodeURIComponent(lim);
  if (uid) url += '&user_id=' + encodeURIComponent(uid);
  const data = await apiFetch(url);
  if (data) populateTable('ev-table', data, ['event_id','kind','target_kind','target_id','payload','created_at']);
}

async function loadClaims() {
  const uid = document.getElementById('cl-user').value.trim();
  if (!uid) { err('user_id is required for claims'); return; }
  const lim = document.getElementById('cl-limit').value.trim() || '100';
  const url = '/viewer/claims.json?user_id=' + encodeURIComponent(uid) + '&limit=' + encodeURIComponent(lim);
  const data = await apiFetch(url);
  if (data) populateTable('cl-table', data, ['claim_id','subject','predicate','object','confidence','state']);
}

async function loadRetrieve() {
  const uid = document.getElementById('rt-user').value.trim();
  if (!uid) { err('user_id is required for retrieve'); return; }
  const q = document.getElementById('rt-q').value.trim();
  const kindRaw = document.getElementById('rt-kind').value;
  const kind = kindRaw === 'by_entity' ? 'by_entity' : kindRaw;
  const url = '/viewer/retrieve.json?user_id=' + encodeURIComponent(uid)
            + '&q=' + encodeURIComponent(q)
            + '&kind=' + encodeURIComponent(kind);
  const data = await apiFetch(url);
  if (data) document.getElementById('rt-out').textContent = JSON.stringify(data, null, 2);
}
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse)
def viewer_index() -> HTMLResponse:
    """Serve the single-page viewer HTML."""
    return HTMLResponse(content=_HTML)


@router.get("/events.json")
def viewer_events(
    request: Request,
    user_id: str | None = Query(None, max_length=128),
    limit: int = Query(100, ge=1, le=1000),
    conn: sqlite3.Connection = _CONN_DEP,
) -> list[dict[str, Any]]:
    """Return events DESC by created_at, scoped to the authed principal.

    Args:
        request: Inbound request — carries ``request.state.user_id`` in
            multi-user mode so the authenticated principal can be bound.
        user_id: Caller-supplied filter. In multi-user mode it is ignored
            in favour of the authenticated principal (a disagreement is
            logged as a leak attempt). In single-token / open mode it is
            used as-is, and is required (omitting it raises 400).
        limit: Max rows to return (default 100, max 1000).
        conn: Injected DB connection.

    Returns:
        List of event dicts with event_id, kind, target_kind, target_id,
        payload, and created_at fields — always scoped to one user.

    Note:
        ``current_user_id`` mirrors the pattern in
        :mod:`parallax.server.routes.query`: in multi-user mode the bound
        principal wins so a caller can only ever read its OWN events; the
        previous unscoped full-table path (which leaked every user's events
        to any valid-token holder) has been removed.
    """
    resolved_user_id = current_user_id(request, user_id)
    rows = conn.execute(
        "SELECT event_id, event_type AS kind, target_kind, target_id, "
        "payload_json AS payload, created_at "
        "FROM events WHERE user_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (resolved_user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/claims.json")
def viewer_claims(
    user_id: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(100, ge=1, le=1000),
    conn: sqlite3.Connection = _CONN_DEP,
) -> list[dict[str, Any]]:
    """Return claims for a user via claims_by_user.

    Args:
        user_id: Required user identifier.
        limit: Max rows (default 100, max 1000).
        conn: Injected DB connection.

    Returns:
        List of claim dicts with subject, predicate, object, confidence,
        and state fields (plus claim_id, user_id, etc.).
    """
    all_claims = claims_by_user(conn, user_id)
    return all_claims[:limit]


@router.get("/retrieve.json")
def viewer_retrieve(
    q: str = Query("", description="query text (subject for by_entity, path for file)"),
    kind: Literal[
        "by_entity", "recent", "file", "decision", "bug", "entity", "timeline"
    ] = Query("by_entity", description="retrieval kind"),
    user_id: str = Query(..., min_length=1, max_length=128),
    conn: sqlite3.Connection = _CONN_DEP,
) -> dict[str, Any]:
    """Run explain_retrieve and return serialized RetrievalTrace.

    Args:
        q: Query text — subject for entity, path for file.
        kind: One of recent, file, decision, bug, entity, timeline. The
            alias ``by_entity`` is normalized to ``entity``.
        user_id: Required user identifier.
        conn: Injected DB connection.

    Returns:
        Serialized :class:`parallax.retrieve.RetrievalTrace` via
        ``dataclasses.asdict``.
    """
    # Normalize the UI alias "by_entity" → "entity"
    resolved_kind = "entity" if kind == "by_entity" else kind
    trace: RetrievalTrace = explain_retrieve(
        conn,
        kind=resolved_kind,
        user_id=user_id,
        query_text=q,
    )
    return dataclasses.asdict(trace)
