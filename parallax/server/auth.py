"""Bearer-token auth for the Parallax HTTP API.

Two modes:

* **Single-token mode (default):** A shared secret ``PARALLAX_TOKEN``
  (env) gates every non-``/healthz`` route. If the env var is unset the
  API runs in *open mode* — intended only for localhost dev / hackathon
  demos. Production deployments must set the token; the app logs a loud
  warning when it boots without one. Routes derive ``user_id`` from the
  request (query param / body field) as today.

* **Multi-user mode:** Enabled by setting ``PARALLAX_MULTI_USER=1`` (or
  ``true``, case-insensitive). The bearer token is a per-user secret
  minted by ``parallax token create``: the server sha256-hashes the
  supplied token and looks it up in the ``api_tokens`` table (m0009).
  A matching, un-revoked row binds the request to its owning
  ``user_id`` — stored on ``request.state.user_id`` so downstream
  routes can scope reads/writes without trusting request-supplied
  identifiers.

The dependency returns the authenticated principal (``"open"``,
``"bearer"``, or ``"user:<uid>"``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from parallax.server.deps import get_conn

__all__ = [
    "require_auth",
    "auth_configured",
    "multi_user_mode",
    "metrics_public_allowed",
    "metrics_auth_required",
    "bind_host_is_safe",
    "assert_safe_to_start",
    "current_user_id",
    "hash_token",
    "bearer_security",
    "PARALLAX_TOKEN_ENV",
    "PARALLAX_MULTI_USER_ENV",
    "PARALLAX_METRICS_PUBLIC_ENV",
    "PARALLAX_BIND_HOST_ENV",
    "PARALLAX_ALLOW_OPEN_PUBLIC_ENV",
]

PARALLAX_TOKEN_ENV = "PARALLAX_TOKEN"
PARALLAX_MULTI_USER_ENV = "PARALLAX_MULTI_USER"
PARALLAX_METRICS_PUBLIC_ENV = "PARALLAX_METRICS_PUBLIC"
PARALLAX_BIND_HOST_ENV = "PARALLAX_BIND_HOST"
PARALLAX_ALLOW_OPEN_PUBLIC_ENV = "PARALLAX_ALLOW_OPEN_PUBLIC"

_LOCALHOST_HOSTS: frozenset[str] = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "[::1]",
    "",  # unset → uvicorn default is 127.0.0.1
})

_log = logging.getLogger("parallax.server.auth")

# auto_error=False so we can distinguish "no Authorization header" (→ 401 we
# own) from a malformed header (→ FastAPI's own 403). The extra control also
# lets us short-circuit in open mode without the header ever being parsed.
bearer_security = HTTPBearer(auto_error=False)
# Backward-compat alias — retained because other modules historically imported
# the private name. Prefer ``bearer_security`` in new code.
_bearer = bearer_security

# Module-level Depends singletons — sidesteps ruff's B008 "function call in
# default argument" complaint for the auth dep wiring.
_BEARER_DEP = Depends(bearer_security)
_CONN_DEP = Depends(get_conn)


def auth_configured() -> bool:
    """True when ``PARALLAX_TOKEN`` is set to a non-empty value."""
    return bool(os.environ.get(PARALLAX_TOKEN_ENV, "").strip())


def multi_user_mode() -> bool:
    """True when ``PARALLAX_MULTI_USER`` selects per-user token auth.

    Accepts ``"1"``, ``"true"`` (case-insensitive). Any other value —
    empty, ``"0"``, ``"false"`` — keeps the server in single-token mode.
    """
    raw = os.environ.get(PARALLAX_MULTI_USER_ENV, "").strip().lower()
    return raw in ("1", "true")


def metrics_public_allowed() -> bool:
    """True when ``PARALLAX_METRICS_PUBLIC=1`` opts /metrics out of auth.

    Set this only when the operator deliberately wants /metrics reachable
    without a bearer token (e.g. behind a private network or Cloudflare
    Access policy). Default is fail-closed: with auth configured, /metrics
    requires the same bearer the rest of the API does.
    """
    raw = os.environ.get(PARALLAX_METRICS_PUBLIC_ENV, "").strip().lower()
    return raw in ("1", "true")


def metrics_auth_required() -> bool:
    """Return True iff the /metrics route should reject anonymous scrapes.

    Auth is required when (a) the public override is *not* set and
    (b) some auth mode is configured (single-token or multi-user). In
    *open mode* (no token, no multi-user) /metrics is open — same posture
    as /healthz — because there is no auth to enforce in the first place.
    """
    if metrics_public_allowed():
        return False
    return auth_configured() or multi_user_mode()


def bind_host_is_safe(host: str | None) -> bool:
    """True when ``host`` resolves to a loopback / unset address."""
    if host is None:
        return True
    return host.strip().lower() in _LOCALHOST_HOSTS


def assert_safe_to_start() -> None:
    """Refuse to start when the server is exposed on a non-localhost
    address without any auth configured.

    Reads ``PARALLAX_BIND_HOST`` (default ``""`` → uvicorn's loopback
    default). Skips the check when ``PARALLAX_ALLOW_OPEN_PUBLIC=1``,
    which is the documented escape hatch for operators who really want an
    open public listener (e.g. private network behind a separate firewall).
    The override path emits a loud audit warning so post-incident log
    readers can tell when the safety net was disabled.

    Raises :class:`RuntimeError` so the failure surfaces during process
    start rather than silently after the listener is up.

    Note on env→bind coupling: ``PARALLAX_BIND_HOST`` is read here, but
    uvicorn binds based on its own ``--host`` argument. The
    ``parallax serve`` CLI subcommand pins them together; operators
    invoking ``uvicorn parallax.server.app:app --host ...`` directly
    (e.g. PM2 ecosystem files) must set ``PARALLAX_BIND_HOST`` themselves
    to match the host, or the safety check is decoupled from reality.
    """
    raw_override = os.environ.get(PARALLAX_ALLOW_OPEN_PUBLIC_ENV, "").strip().lower()
    if raw_override in ("1", "true"):
        bind_host = os.environ.get(PARALLAX_BIND_HOST_ENV, "")
        _log.warning(
            "auth.startup.allow_open_public_override active "
            "(%s=1, %s=%r) — safety check bypassed; ensure the network "
            "boundary protects this listener.",
            PARALLAX_ALLOW_OPEN_PUBLIC_ENV,
            PARALLAX_BIND_HOST_ENV,
            bind_host,
        )
        return
    bind_host = os.environ.get(PARALLAX_BIND_HOST_ENV, "")
    if bind_host_is_safe(bind_host):
        return
    if auth_configured() or multi_user_mode():
        return
    raise RuntimeError(
        f"refusing to start: {PARALLAX_BIND_HOST_ENV}={bind_host!r} is non-localhost "
        f"but neither {PARALLAX_TOKEN_ENV} nor {PARALLAX_MULTI_USER_ENV} is set. "
        f"Set a token, bind to localhost, or set {PARALLAX_ALLOW_OPEN_PUBLIC_ENV}=1 "
        "to opt out (NOT recommended in production)."
    )


def _expected_token() -> str:
    return os.environ.get(PARALLAX_TOKEN_ENV, "").strip()



def hash_token(plaintext: str) -> str:
    """Return the sha256 hex digest of ``plaintext``.

    Used by both the CLI (``parallax token create``) and the auth
    dependency so the hash function stays in one place.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _resolve_multi_user_token(
    request: Request,
    supplied: str,
    conn: sqlite3.Connection,
) -> str:
    """Look up the bearer token's hash in ``api_tokens``.

    Returns the bound ``user_id`` on a match (and sets
    ``request.state.user_id``). Raises 401 on miss / revoked / missing
    table. The lookup hashes the supplied token with sha256 before the
    primary-key query, so a compare_digest on the row is redundant once
    the row is returned.
    """
    supplied_hash = hash_token(supplied)
    try:
        row = conn.execute(
            "SELECT token_hash, user_id, revoked_at FROM api_tokens "
            "WHERE token_hash = ?",
            (supplied_hash,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Table missing — multi-user mode was turned on without
        # running migrations. Treat like an auth miss rather than
        # leaking a 500 that hints at the schema state.
        _log.warning(
            "auth.multi_user.table_missing path=%s", request.url.path
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if row is None:
        _log.warning("auth.reject.unknown path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # sqlite3.Row supports both positional and name access; fall back to
    # index for plain tuple factories (tests sometimes swap).
    user_id = row[1] if not hasattr(row, "keys") else row["user_id"]
    revoked_at = row[2] if not hasattr(row, "keys") else row["revoked_at"]
    if revoked_at is not None:
        _log.warning("auth.reject.revoked path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.user_id = str(user_id)
    return str(user_id)


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = _BEARER_DEP,
    conn: sqlite3.Connection | None = _CONN_DEP,
) -> str:
    """FastAPI dependency: validates the Authorization header.

    * Open mode (``PARALLAX_TOKEN`` unset AND multi-user off) → returns
      ``"open"`` without inspecting the request.
    * Single-token mode → ``Authorization: Bearer <PARALLAX_TOKEN>``
      with a constant-time compare. ``conn`` is not used in this branch.
    * Multi-user mode → bearer → sha256 → ``api_tokens`` lookup; sets
      ``request.state.user_id`` on success. ``conn`` must be non-None.
    """
    if multi_user_mode():
        if creds is None or (creds.scheme or "").lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        supplied = (creds.credentials or "").strip()
        if not supplied:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        assert conn is not None, "multi-user mode requires a DB connection"
        user_id = _resolve_multi_user_token(request, supplied, conn)
        return f"user:{user_id}"

    if not auth_configured():
        return "open"
    if creds is None or (creds.scheme or "").lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = (creds.credentials or "").strip()
    if not hmac.compare_digest(supplied, _expected_token()):
        _log.warning("auth.reject path=%s", request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "bearer"


def current_user_id(request: Request, fallback: str | None) -> str:
    """Return the authenticated ``user_id`` (multi-user) or ``fallback``.

    * When :func:`multi_user_mode` is on and ``require_auth`` stored a
      user_id on ``request.state``, that value wins — request-supplied
      identifiers are ignored (and a warning is logged if they disagree).
    * Otherwise returns ``fallback`` unchanged.
    * Raises 400 when neither is available.

    .. warning::
       **Only multi-user mode is a tenant boundary.** In single-token mode
       (``PARALLAX_TOKEN`` set, ``PARALLAX_MULTI_USER`` off) and in open
       mode (no token), ``request.state.user_id`` is never set, so this
       returns the *client-supplied* ``fallback`` (the ``?user_id`` query
       param / body field) verbatim. Any holder of the single shared token
       — or anyone at all in open mode — can therefore address any
       ``user_id``. Single-token mode is a **single trust domain**, not a
       multi-tenant boundary: run one token per tenant, or set
       ``PARALLAX_MULTI_USER=1`` for genuine per-user isolation. See
       ``docs/deploy.md`` → "Auth modes & tenancy".
    """
    authed = getattr(request.state, "user_id", None)
    if authed:
        if fallback and fallback != authed:
            _log.warning(
                "auth.user_id.override path=%s authed=%s requested=%s",
                request.url.path,
                authed,
                fallback,
            )
        return str(authed)
    if fallback:
        return fallback
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="user_id required",
    )
