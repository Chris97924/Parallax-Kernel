"""``parallax`` command-line entry point.

Subcommands:

    parallax backup  <archive.tar.gz>
    parallax restore <archive.tar.gz> [--no-verify]
    parallax serve   [--host HOST] [--port PORT] [--log-level LEVEL] [--reload]
    parallax inspect events   [--session <id>] [--limit N]
    parallax inspect retrieve "<query>" [--explain] [--level N] [--kind KIND]
    parallax inspect inject   [--session <id>] [--max N]
    parallax router arbitration [--input PATH | --stdin] [--format pretty|jsonl]
    parallax router backfill plan  [--user-id ID]
    parallax router backfill apply [--user-id ID] [--yes]

Reads runtime paths from :func:`parallax.config.load_config` so the same
``PARALLAX_DB_PATH`` / ``PARALLAX_VAULT_PATH`` env vars that drive the rest
of the library also drive the CLI.

Exit codes
----------
* 0 -- success
* 1 -- user-visible error (missing file, archive already exists, bad
        session/query/kind, etc.)
* 2 -- argparse usage error (unknown/missing subcommand)
* 3 -- restore verification mismatch
        (:class:`parallax.restore.RestoreVerificationError`).
* 130 -- interrupted by user (Ctrl+C).
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import pathlib
import sys
from collections.abc import Sequence

from parallax.backup import create_backup, download_from, upload_to
from parallax.config import load_config
from parallax.restore import RestoreVerificationError, restore_backup

__all__ = ["main", "build_parser"]


def _ensure_utf8_streams() -> None:
    """Reconfigure stdout/stderr to UTF-8 so CJK output survives legacy codepages.

    On Windows cmd.exe the default ANSI codepage is often cp950 (Traditional
    Chinese) or cp936 (Simplified). Any print() that contains a CJK character
    then crashes with UnicodeEncodeError long before the user sees output.
    Setting PYTHONIOENCODING in the parent environment only covers subprocesses
    the parent spawns — a human typing ``parallax inspect ...`` directly in cmd
    still hits the crash. Flipping the streams at entry makes the CLI robust
    regardless of parent shell configuration.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        current = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if current == "utf8":
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, LookupError, ValueError, OSError):
            continue


_EXIT_OK = 0
_EXIT_USER_ERROR = 1
_EXIT_USAGE = 2
_EXIT_VERIFY_FAIL = 3
_EXIT_INTERRUPTED = 130


def _silence_broken_pipe(stream: object | None = None) -> None:
    """Redirect the target stream (default stdout) to os.devnull after a BrokenPipeError.

    When the CLI is piped into ``head`` / ``less`` and the reader closes the
    pipe mid-print, Python raises BrokenPipeError — and then its atexit flush
    tries again and re-raises a second traceback during shutdown. Dup'ing
    devnull onto the target stream's fd neutralises the atexit flush so the
    process can exit cleanly. Tolerant of streams without a real fileno
    (pytest capture, io.StringIO) since those only appear in tests.
    """
    target = sys.stdout if stream is None else stream
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, target.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass

_RETRIEVE_KINDS = {"recent", "file", "decision", "bug", "entity", "timeline"}


def _default_user() -> str:
    return os.environ.get("PARALLAX_USER_ID", "chris")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parallax",
        description="Parallax Kernel CLI — backup / restore / inspect the canonical store.",
    )
    sub = parser.add_subparsers(
        dest="command", metavar="{backup,restore,serve,inspect,token,router,canary,ingest}"
    )

    p_backup = sub.add_parser("backup", help="Write a tar.gz backup archive.")
    p_backup.add_argument("archive", type=pathlib.Path, help="destination .tar.gz path")
    p_backup.add_argument(
        "--to",
        dest="upload_to",
        default=None,
        metavar="URI",
        help="upload archive to this URI after creation (e.g. s3://bucket/key or local path); "
             "the local tmp archive is removed after upload",
    )

    p_restore = sub.add_parser("restore", help="Restore from a tar.gz backup archive.")
    p_restore.add_argument("archive", type=pathlib.Path, help="source .tar.gz path")
    p_restore.add_argument(
        "--no-verify",
        action="store_true",
        help="skip manifest verification after restore (default: verify)",
    )
    p_restore.add_argument(
        "--from",
        dest="download_from",
        default=None,
        metavar="URI",
        help="download archive from this URI before restoring (e.g. s3://bucket/key)",
    )

    # ----- serve ------------------------------------------------------------
    # ``parallax serve`` is the canonical launcher: it pins
    # ``PARALLAX_BIND_HOST`` to the same ``--host`` it hands to uvicorn so the
    # safety check in ``parallax.server.auth.assert_safe_to_start`` sees the
    # real bind address. Direct ``uvicorn parallax.server.app:app --host ...``
    # invocation skips this coupling — operators using uvicorn directly must
    # set PARALLAX_BIND_HOST themselves to match (PM2 ecosystem env block).
    p_serve = sub.add_parser(
        "serve",
        help="Run the Parallax FastAPI server (uvicorn).",
    )
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1 — loopback only)",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="bind port (default: 8765)",
    )
    p_serve.add_argument(
        "--log-level",
        dest="log_level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default: info)",
    )
    p_serve.add_argument(
        "--reload",
        action="store_true",
        help="enable uvicorn auto-reload (dev only — never in production)",
    )

    p_inspect = sub.add_parser(
        "inspect", help="Inspect events and retrieval hits for debugging."
    )
    p_inspect.add_argument(
        "--user-id",
        default=None,
        help="user_id to scope queries to (defaults to $PARALLAX_USER_ID or 'chris')",
    )
    isub = p_inspect.add_subparsers(dest="inspect_cmd", metavar="{events,retrieve,inject}")

    p_events = isub.add_parser("events", help="List events for a session.")
    p_events.add_argument("--session", dest="session_id", default=None)
    p_events.add_argument("--limit", type=int, default=20)

    p_retr = isub.add_parser("retrieve", help="Run a retrieval API call and print hits.")
    p_retr.add_argument("query", nargs="?", default="")
    p_retr.add_argument("--explain", action="store_true")
    p_retr.add_argument("--level", type=int, default=1, choices=[1, 2, 3])
    p_retr.add_argument(
        "--kind",
        default=None,
        help=f"retrieval kind; one of {sorted(_RETRIEVE_KINDS)}",
    )
    p_retr.add_argument("--limit", type=int, default=10)
    p_retr.add_argument("--since", default=None)
    p_retr.add_argument("--until", default=None)

    p_inject = isub.add_parser(
        "inject", help="Print a SessionStart <system-reminder> block."
    )
    p_inject.add_argument("--session", dest="session_id", default=None)
    p_inject.add_argument("--max", dest="max_hits", type=int, default=8)

    p_mig = isub.add_parser(
        "migrate",
        help="Show the migration plan (non-destructive).",
    )
    p_mig.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit flag; dry-run is the only mode this subcommand supports",
    )
    p_mig.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit the migration plan as JSON for machine consumption",
    )

    p_token = sub.add_parser(
        "token",
        help="Manage per-user API tokens (multi-user mode).",
    )
    tsub = p_token.add_subparsers(dest="token_cmd", metavar="{create,list,revoke}")

    p_tc = tsub.add_parser("create", help="Mint a new API token for a user.")
    p_tc.add_argument("--user-id", required=True, help="user_id to bind the token to")
    p_tc.add_argument("--label", default=None, help="optional operator-visible label")

    tsub.add_parser("list", help="List known tokens (hashes only — plaintext is never stored).")

    p_tr = tsub.add_parser("revoke", help="Revoke tokens matching a hash prefix.")
    p_tr.add_argument(
        "prefix",
        help="token_hash prefix (>= 6 hex chars); ambiguous matches abort",
    )

    # ----- router -----------------------------------------------------------
    p_router = sub.add_parser(
        "router",
        help="Router-specific tools: arbitration view, backfill plan/apply.",
    )
    rsub = p_router.add_subparsers(dest="router_cmd", metavar="{arbitration,backfill}")

    p_arb = rsub.add_parser(
        "arbitration",
        help="Pretty-print or pass through ArbitrationDecision JSONL.",
    )
    _arb_input = p_arb.add_mutually_exclusive_group()
    _arb_input.add_argument(
        "--input",
        dest="arb_input",
        type=pathlib.Path,
        default=None,
        metavar="PATH",
        help="read ArbitrationDecision JSONL from file (default: --stdin)",
    )
    _arb_input.add_argument(
        "--stdin",
        dest="arb_stdin",
        action="store_true",
        help="read ArbitrationDecision JSONL from stdin (default when --input omitted)",
    )
    p_arb.add_argument(
        "--format",
        dest="arb_format",
        choices=["pretty", "jsonl"],
        default="pretty",
        help="output format: 'pretty' (human-readable) or 'jsonl' (pass-through)",
    )

    p_backfill = rsub.add_parser(
        "backfill",
        help="Backfill plan/apply — see plan and apply subcommands.",
    )
    bsub = p_backfill.add_subparsers(dest="backfill_cmd", metavar="{plan,apply}")

    p_bf_plan = bsub.add_parser(
        "plan",
        help="Dry-run: show unified diff of planned crosswalk upserts.",
    )
    p_bf_plan.add_argument(
        "--user-id",
        default=None,
        help="user_id to scope (default: $PARALLAX_USER_ID or 'chris')",
    )

    p_bf_apply = bsub.add_parser(
        "apply",
        help="Execute crosswalk upserts (requires --yes or stdin CONFIRM).",
    )
    p_bf_apply.add_argument(
        "--user-id",
        default=None,
        help="user_id to scope (default: $PARALLAX_USER_ID or 'chris')",
    )
    p_bf_apply.add_argument(
        "--yes",
        action="store_true",
        help="skip the stdin CONFIRM gate and proceed immediately",
    )

    # ----- canary -----------------------------------------------------------
    # Imported lazily so canary side-effects (sqlite3 imports, etc.) only
    # land when the user actually invokes `parallax canary ...`.
    from parallax.canary.cli import register_canary_subparser

    register_canary_subparser(sub)

    # ----- ingest -----------------------------------------------------------
    p_ingest = sub.add_parser(
        "ingest",
        help="Ingest a .aphelion.tar package — writes one audit row per claim.",
    )
    p_ingest.add_argument(
        "package_path",
        type=pathlib.Path,
        help="Path to .aphelion.tar (must resolve under PARALLAX_APHELION_PACKAGE_DIR)",
    )
    p_ingest.add_argument(
        "--audit-db",
        type=pathlib.Path,
        default=None,
        help=(
            "Override PARALLAX_AUDIT_DB_PATH for this invocation (testing). "
            "Defensive guard rejects 'parallax-kernel/db'."
        ),
    )
    p_ingest.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run all validation + verify_package + claim extract but DO NOT write audit rows. "
            "Returns 0 on success, non-zero on validation failure."
        ),
    )

    return parser


# ----- backup / restore -----------------------------------------------------


def _cmd_backup(archive: pathlib.Path, *, upload_uri: str | None = None) -> int:
    cfg = load_config()
    try:
        manifest = create_backup(cfg, archive)
    except (FileNotFoundError, FileExistsError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return _EXIT_USER_ERROR
    print(
        f"Wrote backup: {archive}\n"
        f"  db_sha256: {manifest.db_sha256}\n"
        f"  row_counts: {manifest.row_counts}"
    )
    if upload_uri is not None:
        try:
            upload_to(archive, upload_uri)
        except (ImportError, ValueError, OSError) as exc:
            print(f"upload failed: {exc}", file=sys.stderr)
            return _EXIT_USER_ERROR
        print(f"  uploaded to: {upload_uri}")
        try:
            archive.unlink()
        except OSError:
            pass
    return _EXIT_OK


def _cmd_restore(
    archive: pathlib.Path,
    *,
    verify: bool,
    download_uri: str | None = None,
) -> int:
    cfg = load_config()
    if download_uri is not None:
        try:
            download_from(download_uri, archive)
        except (ImportError, ValueError, OSError) as exc:
            print(f"download failed: {exc}", file=sys.stderr)
            return _EXIT_USER_ERROR
        print(f"  downloaded from: {download_uri}")
    try:
        manifest = restore_backup(cfg, archive, verify=verify)
    except FileNotFoundError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return _EXIT_USER_ERROR
    except RestoreVerificationError as exc:
        print(f"restore verification failed:\n{exc}", file=sys.stderr)
        return _EXIT_VERIFY_FAIL
    print(
        f"Restored from: {archive}\n"
        f"  verified: {verify}\n"
        f"  row_counts: {manifest.row_counts}"
    )
    return _EXIT_OK


# ----- inspect --------------------------------------------------------------


def _open_conn():
    cfg = load_config()
    from parallax.sqlite_store import connect
    return connect(cfg.db_path)


def _cmd_inspect_events(*, user_id: str, session_id: str | None, limit: int) -> int:
    conn = _open_conn()
    try:
        if session_id is None:
            rows = conn.execute(
                "SELECT session_id, COUNT(*) AS n FROM events "
                "WHERE user_id = ? AND session_id IS NOT NULL "
                "GROUP BY session_id ORDER BY MAX(created_at) DESC LIMIT 5",
                (user_id,),
            ).fetchall()
            if not rows:
                print("(no sessions found)")
                return _EXIT_OK
            print("Recent sessions:")
            for r in rows:
                print(f"  {r['session_id']}  events={r['n']}")
            return _EXIT_OK

        rows = conn.execute(
            "SELECT created_at, event_type, target_kind, target_id, payload_json "
            "FROM events WHERE user_id = ? AND session_id = ? "
            "ORDER BY created_at ASC LIMIT ?",
            (user_id, session_id, limit),
        ).fetchall()
        if not rows:
            print(f"no events for session {session_id!r}", file=sys.stderr)
            return _EXIT_USER_ERROR
        for r in rows:
            summary = (r["payload_json"] or "")[:120].replace("\n", " ")
            tk = r["target_kind"] or "-"
            ti = r["target_id"] or "-"
            print(f"{r['created_at']}  {r['event_type']:<22}  {tk}:{ti}  {summary}")
        return _EXIT_OK
    finally:
        conn.close()


def _pick_retrieve_kind(query: str, kind: str | None) -> str:
    if kind is not None:
        return kind
    return "recent" if not query else "entity"


def _format_kv(d: dict) -> str:
    """Compact ``k=v, k=v`` rendering with stable sort for debug output."""
    return ", ".join(f"{k}={v!r}" for k, v in sorted(d.items()))


def _print_trace_header(trace) -> None:
    """Print the per-query trace block for `parallax inspect retrieve --explain`.

    Kept separate from hit rendering so the zero-hit path still sees the
    header — that's the whole point of the --explain rail for LongMemEval
    debug. Reads the frozen :class:`RetrievalTrace` produced by
    :func:`parallax.retrieve.explain_retrieve`.
    """
    print(f"== trace(kind={trace.kind}) ==")
    print(f"params: {_format_kv(trace.params)}")
    if trace.normalized_params:
        print(f"normalized: {_format_kv(trace.normalized_params)}")
    for sql in trace.sql_fragments:
        print(f"sql: {sql}")
    if trace.stages:
        print("stages:")
        for s in trace.stages:
            detail = f" {s.detail}" if s.detail else ""
            print(
                f"  {s.name}: {s.candidates_in}->{s.candidates_out}{detail}"
            )
    if trace.notes:
        print("notes:")
        for n in trace.notes:
            print(f"  - {n}")


def _cmd_inspect_retrieve(
    *,
    user_id: str,
    query: str,
    kind: str | None,
    explain: bool,
    level: int,
    limit: int,
    since: str | None,
    until: str | None,
) -> int:
    from parallax import retrieve as R

    resolved_kind = _pick_retrieve_kind(query, kind)
    if resolved_kind not in _RETRIEVE_KINDS:
        print(
            f"unknown --kind {resolved_kind!r}; expected one of {sorted(_RETRIEVE_KINDS)}",
            file=sys.stderr,
        )
        return _EXIT_USER_ERROR
    if resolved_kind == "timeline" and (since is None or until is None):
        print("timeline kind requires --since and --until", file=sys.stderr)
        return _EXIT_USER_ERROR

    conn = _open_conn()
    try:
        if explain:
            try:
                trace = R.explain_retrieve(
                    conn,
                    kind=resolved_kind,
                    user_id=user_id,
                    query_text=query,
                    limit=limit,
                    since=since,
                    until=until,
                )
            except ValueError as exc:
                print(f"bad retrieval args: {exc}", file=sys.stderr)
                return _EXIT_USER_ERROR
            _print_trace_header(trace)
            hits = list(trace.hits)
        else:
            if resolved_kind == "recent":
                hits = R.recent_context(conn, user_id=user_id, limit=limit)
            elif resolved_kind == "file":
                hits = R.by_file(conn, user_id=user_id, path=query, limit=limit)
            elif resolved_kind == "decision":
                hits = R.by_decision(conn, user_id=user_id, limit=limit)
            elif resolved_kind == "bug":
                hits = R.by_bug_fix(conn, user_id=user_id, limit=limit)
            elif resolved_kind == "entity":
                hits = R.by_entity(conn, user_id=user_id, subject=query, limit=limit)
            else:  # timeline — since/until pre-checked above
                try:
                    hits = R.by_timeline(
                        conn,
                        user_id=user_id,
                        since=since,
                        until=until,
                        limit=limit,
                    )
                except ValueError as exc:
                    print(f"bad timeline window: {exc}", file=sys.stderr)
                    return _EXIT_USER_ERROR

        if not hits:
            print("(no hits)")
            return _EXIT_OK

        for h in hits:
            proj = h.project(level)
            print(
                f"[{proj['entity_kind']}:{proj['entity_id']}] "
                f"score={proj['score']:.3f}  {proj['title']}"
            )
            if level >= 2 and proj.get("evidence"):
                print(f"    evidence: {proj['evidence']}")
            if level >= 3:
                print(f"    full: {proj.get('full')}")
            if explain:
                print(f"    reason: {h.explain.get('reason','')}")
                print(f"    score_components: {h.explain.get('score_components', {})}")
        return _EXIT_OK
    finally:
        conn.close()


def _cmd_inspect_migrate(*, as_json: bool) -> int:
    import json as _json

    from parallax.migrations import MIGRATIONS, migration_plan

    conn = _open_conn()
    try:
        plan = migration_plan(conn)
    finally:
        conn.close()

    if as_json:
        payload = {
            "applied": list(plan.applied),
            "current_version": plan.current_version,
            "target_version": plan.target_version,
            "pending": [
                {
                    "version": s.version,
                    "name": s.name,
                    "statements": list(s.statements),
                    "row_impact_estimates": s.row_impact_estimates,
                }
                for s in plan.pending
            ],
        }
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return _EXIT_OK

    applied_set = set(plan.applied)
    print(f"{'version':>7}  {'name':<26}  {'status':<8}  est_row_impact")
    for mig in sorted(MIGRATIONS, key=lambda m: m.version):
        if mig.version in applied_set:
            status = "applied"
            impact = "-"
        else:
            status = "pending"
            step = next(
                (s for s in plan.pending if s.version == mig.version), None
            )
            impact = (
                ",".join(
                    f"{t}={n}" for t, n in sorted(step.row_impact_estimates.items())
                )
                if step
                else "-"
            )
        print(f"{mig.version:>7}  {mig.name:<26}  {status:<8}  {impact}")
    return _EXIT_OK


def _cmd_inspect_inject(*, user_id: str, session_id: str | None, max_hits: int) -> int:
    from parallax.injector import build_session_reminder

    conn = _open_conn()
    try:
        text = build_session_reminder(
            conn, user_id=user_id, session_id=session_id, max_hits=max_hits
        )
        print(text)
        return _EXIT_OK
    finally:
        conn.close()


# ----- token management -----------------------------------------------------


_TOKEN_MIN_PREFIX = 6


def _cmd_token_create(*, user_id: str, label: str | None) -> int:
    """Mint a fresh token, hash+store it, and print the plaintext ONCE.

    Uses :func:`secrets.token_urlsafe` for the plaintext (32 random bytes
    → ~43 URL-safe chars) and ``sha256`` for the stored digest. The
    plaintext is discarded after printing — a lost token must be revoked
    and re-issued, matching GitHub / PyPI token semantics.
    """
    import secrets as _secrets

    from parallax.server.auth import hash_token
    from parallax.sqlite_store import now_iso

    plaintext = _secrets.token_urlsafe(32)
    token_hash = hash_token(plaintext)
    conn = _open_conn()
    try:
        try:
            conn.execute(
                "INSERT INTO api_tokens(token_hash, user_id, created_at, "
                "revoked_at, label) VALUES (?, ?, ?, NULL, ?)",
                (token_hash, user_id, now_iso(), label),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — surface as user error
            print(f"token create failed: {exc}", file=sys.stderr)
            return _EXIT_USER_ERROR
    finally:
        conn.close()
    print(plaintext)
    print("  token_hash_prefix: " + token_hash[:12], file=sys.stderr)
    print(
        "  WARNING: copy the token above now — it is not shown again.",
        file=sys.stderr,
    )
    return _EXIT_OK


def _cmd_token_list() -> int:
    conn = _open_conn()
    try:
        try:
            rows = conn.execute(
                "SELECT token_hash, user_id, created_at, revoked_at, label "
                "FROM api_tokens ORDER BY created_at ASC"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            print(f"token list failed: {exc}", file=sys.stderr)
            return _EXIT_USER_ERROR
    finally:
        conn.close()
    if not rows:
        print("(no tokens)")
        return _EXIT_OK
    print(
        f"{'prefix':<12}  {'user_id':<24}  {'created_at':<32}  "
        f"{'revoked_at':<32}  label"
    )
    for r in rows:
        prefix = str(r["token_hash"])[:12]
        revoked = r["revoked_at"] or "-"
        label = r["label"] or ""
        print(
            f"{prefix:<12}  {r['user_id']:<24}  {r['created_at']:<32}  "
            f"{revoked:<32}  {label}"
        )
    return _EXIT_OK


def _cmd_token_revoke(prefix: str) -> int:
    from parallax.sqlite_store import now_iso

    if len(prefix) < _TOKEN_MIN_PREFIX:
        print(
            f"refusing to revoke by prefix shorter than {_TOKEN_MIN_PREFIX} chars "
            "— pass more of the token_hash",
            file=sys.stderr,
        )
        return _EXIT_USER_ERROR
    # token_hash is sha256 hex; reject non-hex prefixes to kill LIKE-injection
    # via % / _ metacharacters.
    if not all(c in "0123456789abcdef" for c in prefix.lower()):
        print(
            f"invalid prefix {prefix!r}: token hashes are hex [0-9a-f]",
            file=sys.stderr,
        )
        return _EXIT_USER_ERROR
    conn = _open_conn()
    try:
        try:
            rows = conn.execute(
                "SELECT token_hash, user_id, revoked_at FROM api_tokens "
                "WHERE token_hash LIKE ?",
                (prefix + "%",),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            print(f"token revoke failed: {exc}", file=sys.stderr)
            return _EXIT_USER_ERROR
        if not rows:
            print(f"no tokens match prefix {prefix!r}", file=sys.stderr)
            return _EXIT_USER_ERROR
        if len(rows) > 1:
            print(
                f"prefix {prefix!r} matches {len(rows)} tokens; refusing to revoke",
                file=sys.stderr,
            )
            for r in rows:
                print(
                    f"  {str(r['token_hash'])[:12]}  {r['user_id']}",
                    file=sys.stderr,
                )
            return _EXIT_USER_ERROR
        (row,) = rows
        if row["revoked_at"] is not None:
            print(
                f"token {str(row['token_hash'])[:12]} already revoked at "
                f"{row['revoked_at']}"
            )
            return _EXIT_OK
        conn.execute(
            "UPDATE api_tokens SET revoked_at = ? WHERE token_hash = ?",
            (now_iso(), row["token_hash"]),
        )
        conn.commit()
    finally:
        conn.close()
    print(f"revoked {str(row['token_hash'])[:12]}  user_id={row['user_id']}")
    return _EXIT_OK


# ----- router: arbitration --------------------------------------------------


def _fmt_arbitration_pretty(d: dict) -> str:
    """Format one ArbitrationDecision JSON dict as a human-readable line."""
    canonical_ref = d.get("canonical_field", "-")
    state = d.get("state", "-")
    selected = d.get("selected") or {}
    source_id = selected.get("source", "-") if isinstance(selected, dict) else "-"
    candidates = d.get("candidates") or []
    evidence_count = len(candidates)
    # Column order matches ArbitrationDecision dataclass field order:
    # canonical_field, state, selected (source), candidates (evidence_count).
    return (
        f"canonical_ref={canonical_ref}"
        f"  decided_state={state}"
        f"  source={source_id}"
        f"  evidence_count={evidence_count}"
    )


def _cmd_router_arbitration(
    *,
    arb_input: pathlib.Path | None,
    arb_format: str,
) -> int:
    """Stream ArbitrationDecision JSONL from file or stdin and render it."""
    if arb_input is not None:
        try:
            lines: list[str] = arb_input.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"cannot read {arb_input}: {exc}", file=sys.stderr)
            return _EXIT_USER_ERROR
    else:
        lines = sys.stdin.read().splitlines()

    if not lines:
        return _EXIT_OK

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        if arb_format == "jsonl":
            print(raw)
            continue
        # pretty format
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"warning: skipping malformed JSONL line: {exc}", file=sys.stderr)
            continue
        print(_fmt_arbitration_pretty(d))
    return _EXIT_OK


# ----- router: backfill plan / apply ----------------------------------------


def _crosswalk_rows_for_user(conn, user_id: str) -> list[dict]:
    """Read existing crosswalk rows for a user, sorted by canonical_ref."""
    rows = conn.execute(
        "SELECT canonical_ref, parallax_target_kind, parallax_target_id, state, query_type "
        "FROM crosswalk WHERE user_id = ? ORDER BY canonical_ref ASC",
        (user_id,),
    ).fetchall()
    return [
        {
            "canonical_ref": r["canonical_ref"],
            "target_kind": r["parallax_target_kind"],
            "target_id": r["parallax_target_id"],
            "state": r["state"],
            "query_type": r["query_type"],
        }
        for r in rows
    ]


def _fmt_crosswalk_line(row: dict) -> str:
    qt = row.get("query_type") or "None"
    return (
        f"canonical_ref={row['canonical_ref']}"
        f"  target={row.get('target_kind','-')}:{row.get('target_id','-')}"
        f"  state={row.get('state','-')}"
        f"  query_type={qt}"
    )


def _cmd_router_backfill_plan(*, user_id: str) -> int:
    from parallax.router.backfill import BackfillRunner

    conn = _open_conn()
    try:
        before = _crosswalk_rows_for_user(conn, user_id)
        runner = BackfillRunner(conn)
        planned = runner.plan_upserts(user_id, scope="all")
    finally:
        conn.close()

    before_lines = [_fmt_crosswalk_line(r) for r in before]

    # Merge planned into current state: update existing rows, append new ones.
    before_by_ref = {r["canonical_ref"]: r for r in before}
    after_dict = dict(before_by_ref)
    for p in planned:
        after_dict[p["canonical_ref"]] = p
    after_lines = [
        _fmt_crosswalk_line(r)
        for r in sorted(after_dict.values(), key=lambda r: r["canonical_ref"])
    ]

    diff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="crosswalk/current",
            tofile="crosswalk/planned",
            lineterm="",
        )
    )
    if not diff:
        print("(no changes planned)")
    else:
        print("\n".join(diff))
    return _EXIT_OK


def _cmd_router_backfill_apply(*, user_id: str, yes: bool) -> int:
    from parallax.router import is_router_enabled
    from parallax.router.backfill import BackfillRunner
    from parallax.router.contracts import BackfillRequest
    from parallax.router.crosswalk_seed import seed_hash

    if not is_router_enabled():
        print("backfill apply requires MEMORY_ROUTER=true", file=sys.stderr)
        return _EXIT_USER_ERROR

    if not yes:
        print(
            "Type CONFIRM to execute crosswalk upserts, or anything else to abort:",
            file=sys.stderr,
        )
        try:
            answer = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "CONFIRM":
            print(
                "plan required first; use plan subcommand",
                file=sys.stderr,
            )
            return _EXIT_USER_ERROR

    conn = _open_conn()
    try:
        request = BackfillRequest(
            user_id=user_id,
            crosswalk_version=seed_hash(),
            dry_run=False,
            scope="all",
        )
        report = BackfillRunner(conn).run(request)
        conn.commit()
    except (ValueError, RuntimeError) as exc:
        print(f"backfill apply failed: {exc}", file=sys.stderr)
        return _EXIT_USER_ERROR
    finally:
        conn.close()

    print(
        f"backfill complete: examined={report.rows_examined}"
        f"  mapped={report.rows_mapped}"
        f"  unmapped={report.rows_unmapped}"
        f"  writes={report.writes_performed}"
    )
    return _EXIT_OK


# ----- ingest --------------------------------------------------------------


def _cmd_ingest(
    *,
    package_path: pathlib.Path,
    audit_db_override: pathlib.Path | None,
    dry_run: bool,
) -> int:
    """Ingest a .aphelion.tar package into the audit DB.

    ``--dry-run`` mode opens the real audit DB (so spec §4 startup gates run
    via ``validate=True``) but routes ``ingest_package`` writes to an
    in-memory SQLite clone seeded with the same audit-row schema. No
    SAVEPOINT is used — ``write_audit_row`` requires an autocommit
    connection (``in_transaction == False``) and raises
    ``AuditDbUsageError`` otherwise, so we must hand the writer a clean
    autocommit conn. The production audit DB is never written to in
    ``--dry-run``; the in-memory conn is closed at the end and discarded.
    Structured-log shape follows spec §6.4 (``package_path`` /
    ``signer_id`` / ``underlying``); the older ``path`` field name was
    drift from the spec and has been removed.
    """
    import sqlite3  # noqa: PLC0415 — lazy import; ingest-only
    import time  # noqa: PLC0415 — lazy import; ingest-only
    import uuid  # noqa: PLC0415 — lazy import; ingest-only

    from parallax.apex.aphelion_ingest import (  # noqa: PLC0415 — lazy import; ingest-only
        ParallaxIngestError,
        ingest_package,
    )
    from parallax.apex.audit_db import (  # noqa: PLC0415 — lazy import; ingest-only
        _SCHEMA_STATEMENTS,
        AuditDbConfigError,
        open_audit_db,
    )
    from parallax.obs.log import get_logger  # noqa: PLC0415 — lazy import; ingest-only

    _log = get_logger(__name__)
    pkg_path_str = str(package_path)

    # --- Resolve env vars (fail loud on missing, per rules/python/security.md) ---
    try:
        pkg_dir_raw = os.environ["PARALLAX_APHELION_PACKAGE_DIR"]
    except KeyError:
        _log.error(
            "parallax_ingest_failed",
            extra={
                "event": "parallax_ingest_failed",
                "reason_code": "pkg.dir_unset",
                "package_path": pkg_path_str,
                "signer_id": "",
                "underlying": "PARALLAX_APHELION_PACKAGE_DIR env var is unset",
            },
        )
        return 78  # EX_CONFIG

    try:
        trust_store_raw = os.environ["PARALLAX_APHELION_TRUST_STORE"]
    except KeyError:
        _log.error(
            "parallax_ingest_failed",
            extra={
                "event": "parallax_ingest_failed",
                "reason_code": "pkg.trust_store_missing",
                "package_path": pkg_path_str,
                "signer_id": "",
                "underlying": "PARALLAX_APHELION_TRUST_STORE env var is unset",
            },
        )
        return 78  # EX_CONFIG

    # Resolve audit DB path: --audit-db override takes precedence over env var.
    if audit_db_override is not None:
        audit_db_path = audit_db_override
    else:
        try:
            audit_db_path = pathlib.Path(os.environ["PARALLAX_AUDIT_DB_PATH"])
        except KeyError:
            _log.error(
                "parallax_ingest_failed",
                extra={
                    "event": "parallax_ingest_failed",
                    "reason_code": "disk.audit_db_unset",
                    "package_path": pkg_path_str,
                    "signer_id": "",
                    "underlying": "PARALLAX_AUDIT_DB_PATH env var is unset",
                },
            )
            return 78  # EX_CONFIG

    # Defensive --audit-db guard (issues #63 + #64): when --audit-db is set,
    # reject any override path containing the ``parallax-kernel/db`` substring
    # *unconditionally* — including outside --dry-run. The previous
    # dry-run-scoped guard left plain ``parallax ingest <pkg> --audit-db
    # /home/chris/parallax-kernel/db/audit.db`` invocations unprotected against
    # operator typos that would have written live ingest rows into the
    # production audit DB. ``--audit-db`` is documented as a testing override
    # in the impl spec, so blocking the production-DB path under any usage is
    # consistent with intent. The reason_code is ``disk.audit_db_unsafe_path``
    # mapped to exit 78 (EX_CONFIG) via _REASON_TO_EXIT, so the structured-log
    # tag and the process exit agree (was the binary inconsistency at #64).
    # ``as_posix()`` normalizes path separators so the substring guard
    # is portable across Windows (which renders forward slashes as
    # backslashes via ``str(Path(...))``) and POSIX hosts. Matters for
    # the test suite and for Windows-side operator invocations.
    if (
        audit_db_override is not None
        and "parallax-kernel/db" in audit_db_path.as_posix()
    ):
        _log.error(
            "parallax_ingest_failed",
            extra={
                "event": "parallax_ingest_failed",
                "reason_code": "disk.audit_db_unsafe_path",
                "package_path": pkg_path_str,
                "signer_id": "",
                "underlying": (
                    f"production audit.db rejected by --audit-db guard: "
                    f"{audit_db_path}"
                ),
            },
        )
        raise SystemExit(78)

    # --- Open audit DB (validate=True runs §4 startup gates on the real DB) ---
    # Codex round-2 P1: catch raw sqlite3.Error from PRAGMA/quick_check/schema
    # apply, not just AuditDbConfigError. Both surface as operator-config
    # failures (broken DB file, readonly/locked/corrupt) and MUST exit
    # EX_CONFIG (78) per spec §6.3, not the generic exit-1 top-level path.
    try:
        audit_conn = open_audit_db(audit_db_path, validate=True)
    except (AuditDbConfigError, sqlite3.Error) as exc:
        _log.error(
            "parallax_ingest_failed",
            extra={
                "event": "parallax_ingest_failed",
                "reason_code": (
                    "disk.audit_db_unset"
                    if isinstance(exc, AuditDbConfigError)
                    else "disk.permission"
                ),
                "package_path": pkg_path_str,
                "signer_id": "",
                "underlying": f"{type(exc).__name__}: {exc}"[:256],
            },
        )
        return 78  # EX_CONFIG

    # --- Dry-run: route writes to an in-memory clone with the same schema. ---
    # P1-1 fix: previous SAVEPOINT approach was incompatible with
    # write_audit_row, which requires conn.in_transaction == False
    # (raises AuditDbUsageError otherwise — see audit_db.py:540). The
    # in-memory clone keeps the production DB untouched while still
    # exercising the full ingest path through canonicalize_row +
    # write_audit_row. We close the on-disk conn first so dry-run never
    # holds two handles open against the real DB at once.
    write_conn = audit_conn
    if dry_run:
        audit_conn.close()
        write_conn = sqlite3.connect(":memory:", isolation_level=None)
        write_conn.row_factory = sqlite3.Row
        for stmt in _SCHEMA_STATEMENTS:
            write_conn.execute(stmt)

    try:
        session_id = f"ingest:{int(time.time())}:{uuid.uuid4()}"
        try:
            report = ingest_package(
                package_path=package_path,
                audit_conn=write_conn,
                package_dir=pathlib.Path(pkg_dir_raw),
                trust_store_dir=pathlib.Path(trust_store_raw),
                session_id=session_id,
            )
        except ParallaxIngestError as exc:
            _log.error(
                "parallax_ingest_failed",
                extra={
                    "event": "parallax_ingest_failed",
                    "reason_code": exc.reason_code,
                    "package_path": pkg_path_str,
                    "signer_id": "",
                    "underlying": f"{type(exc).__name__}: {exc}"[:256],
                },
            )
            return exc.exit_code
    finally:
        write_conn.close()

    _log.info(
        "parallax_ingest_succeeded",
        extra={
            "event": "parallax_ingest_succeeded",
            "claim_count": report.claims_ingested,
            "audit_rows_written": 0 if dry_run else report.audit_rows_written,
            "signer_id": report.signer_id,
            "package_id": report.package_id,
            "package_path": pkg_path_str,
            "dry_run": dry_run,
        },
    )
    return 0


# ----- dispatcher -----------------------------------------------------------


def _cmd_serve(*, host: str, port: int, log_level: str, reload: bool) -> int:
    """Run the FastAPI server via uvicorn.

    Pins ``PARALLAX_BIND_HOST`` to the same ``--host`` we pass to uvicorn so
    that ``assert_safe_to_start()`` (called inside ``create_app`` at module
    import) sees the actual bind address. Without this pin, an operator
    could run ``parallax serve --host 0.0.0.0`` while ``PARALLAX_BIND_HOST``
    is unset, bypassing the safety check while uvicorn still binds publicly.
    """
    # Set the env BEFORE we import uvicorn / parallax.server.app — module
    # import triggers create_app(), which reads the env. setdefault preserves
    # an explicit operator-supplied value (paranoid override).
    os.environ.setdefault("PARALLAX_BIND_HOST", host)
    if os.environ["PARALLAX_BIND_HOST"] != host:
        print(
            f"warning: PARALLAX_BIND_HOST={os.environ['PARALLAX_BIND_HOST']!r} "
            f"differs from --host {host!r}; the safety check will see the "
            f"env value, but uvicorn will bind to {host!r}. Operators must "
            "keep these in sync.",
            file=sys.stderr,
        )
    try:
        import uvicorn  # noqa: PLC0415 — lazy import; only needed for serve
    except ImportError as exc:
        print(
            f"parallax serve requires uvicorn: install with "
            f"`pip install '.[server]'` ({exc})",
            file=sys.stderr,
        )
        return _EXIT_USER_ERROR

    # Apex M5 audit-db preflight (spec §4): validate PARALLAX_AUDIT_DB_PATH
    # BEFORE uvicorn binds. parallax.server.lifespan re-runs this same check
    # and stashes app.state.audit_db_path for the write path — but a
    # lifespan-raised exception is swallowed by uvicorn into a non-78 process
    # exit, so this preflight is what makes the canonical launcher exit with
    # the deterministic EX_CONFIG (78) on a broken audit path.
    import contextlib  # noqa: PLC0415 — lazy import; serve-only
    import sqlite3  # noqa: PLC0415 — lazy import; serve-only

    from parallax.apex.audit_db import (  # noqa: PLC0415 — lazy import; serve-only
        EX_CONFIG,
        AuditDbConfigError,
        open_audit_db,
        resolve_audit_db_path,
    )

    try:
        # contextlib.closing releases the preflight connection even if a later
        # line raises — open_audit_db runs the §4 gates; nothing else to do.
        with contextlib.closing(open_audit_db(resolve_audit_db_path(), validate=True)):
            pass
    except AuditDbConfigError as exc:
        print(f"parallax serve: audit-db preflight failed: {exc}", file=sys.stderr)
        return EX_CONFIG
    except sqlite3.Error as exc:
        # Codex 2026-05-15 round-2 P2: open_audit_db wraps the initial
        # sqlite3.connect() in AuditDbConfigError, but the subsequent PRAGMA
        # and schema-apply steps re-raise raw sqlite3.Error subclasses. A
        # readonly / corrupt / partially-locked DB therefore terminates with
        # an uncaught traceback instead of the deterministic EX_CONFIG (78)
        # the launcher contract promises. Convert here so startup error
        # handling stays consistent regardless of which gate raised.
        print(
            f"parallax serve: audit-db preflight failed: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return EX_CONFIG

    uvicorn.run(
        "parallax.server.app:app",
        host=host,
        port=port,
        log_level=log_level,
        reload=reload,
    )
    return _EXIT_OK


def _dispatch(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "backup":
        return _cmd_backup(args.archive, upload_uri=args.upload_to)
    if args.command == "restore":
        return _cmd_restore(
            args.archive,
            verify=not args.no_verify,
            download_uri=args.download_from,
        )
    if args.command == "serve":
        return _cmd_serve(
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            reload=args.reload,
        )
    if args.command == "token":
        if args.token_cmd == "create":
            return _cmd_token_create(user_id=args.user_id, label=args.label)
        if args.token_cmd == "list":
            return _cmd_token_list()
        if args.token_cmd == "revoke":
            return _cmd_token_revoke(args.prefix)
        parser.parse_args(["token", "--help"])
        return _EXIT_USAGE
    if args.command == "router":
        if args.router_cmd == "arbitration":
            return _cmd_router_arbitration(
                arb_input=args.arb_input,
                arb_format=args.arb_format,
            )
        if args.router_cmd == "backfill":
            user_id = args.user_id if args.user_id is not None else _default_user()
            if args.backfill_cmd == "plan":
                return _cmd_router_backfill_plan(user_id=user_id)
            if args.backfill_cmd == "apply":
                return _cmd_router_backfill_apply(user_id=user_id, yes=args.yes)
            parser.parse_args(["router", "backfill", "--help"])
            return _EXIT_USAGE
        parser.parse_args(["router", "--help"])
        return _EXIT_USAGE
    if args.command == "canary":
        from parallax.canary.cli import cmd_canary

        return cmd_canary(args)
    if args.command == "ingest":
        return _cmd_ingest(
            package_path=args.package_path,
            audit_db_override=args.audit_db,
            dry_run=args.dry_run,
        )
    if args.command == "inspect":
        user_id = args.user_id if args.user_id is not None else _default_user()
        if args.inspect_cmd == "events":
            return _cmd_inspect_events(
                user_id=user_id,
                session_id=args.session_id,
                limit=args.limit,
            )
        if args.inspect_cmd == "retrieve":
            return _cmd_inspect_retrieve(
                user_id=user_id,
                query=args.query,
                kind=args.kind,
                explain=args.explain,
                level=args.level,
                limit=args.limit,
                since=args.since,
                until=args.until,
            )
        if args.inspect_cmd == "inject":
            return _cmd_inspect_inject(
                user_id=user_id,
                session_id=args.session_id,
                max_hits=args.max_hits,
            )
        if args.inspect_cmd == "migrate":
            return _cmd_inspect_migrate(as_json=args.as_json)
        parser.parse_args(["inspect", "--help"])
        return _EXIT_USAGE
    parser.print_help(sys.stderr)
    return _EXIT_USAGE


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _ensure_utf8_streams()
        return _dispatch(argv)
    except KeyboardInterrupt:
        return _EXIT_INTERRUPTED
    except BrokenPipeError:
        _silence_broken_pipe()
        return _EXIT_OK
    except Exception as exc:  # noqa: BLE001 — last-resort CLI guard
        try:
            print(f"parallax: {exc}", file=sys.stderr)
        except BrokenPipeError:
            _silence_broken_pipe(sys.stderr)
        return _EXIT_USER_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
