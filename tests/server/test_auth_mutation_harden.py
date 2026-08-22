"""Mutation-hardening for ``parallax.server.auth`` (land-20260823 wave 4, S1).

Additive companion to ``tests/server/test_server_safety.py`` and
``tests/server/test_multi_user_auth.py``. Every test below was written against a
semantic mutant of ``parallax/server/auth.py`` that the pre-existing suite let
through. Where that suite is thin:

  * **The env flags are read at their centre, never at their edges.** Every
    pre-existing case sets ``PARALLAX_MULTI_USER`` / ``PARALLAX_METRICS_PUBLIC``
    / ``PARALLAX_ALLOW_OPEN_PUBLIC`` to exactly ``"1"``, or deletes it. The
    ``.strip()``, the ``.lower()``, and — most importantly — the *closed*
    membership test that stops ``"0"`` / ``"false"`` / ``"off"`` from switching a
    safety net on are all unobserved. A flag parser widened to "any non-empty
    value means on" keeps the whole suite green while
    ``PARALLAX_ALLOW_OPEN_PUBLIC=0`` silently disables the public-bind guard.

  * **/metrics posture is only ever proven for single-token mode.** No case
    configures multi-user mode alone, so dropping ``multi_user_mode()`` from
    ``metrics_auth_required``'s disjunction leaves a multi-user deployment's
    /metrics open to anonymous scrapes with nothing turning red.

  * **The two auth modes are never enabled at once.** ``mu_app`` deletes
    ``PARALLAX_TOKEN``; ``auth_app`` deletes ``PARALLAX_MULTI_USER``. Their
    precedence is therefore unpinned, and a mutant that consults the shared
    secret first hands every multi-user deployment a master key.

  * **The bearer credential is always well-formed.** ``HTTPBearer`` filters
    non-bearer schemes before the dependency runs, so the module's own scheme
    check and its whitespace-only-credential branch are unreachable through
    ``TestClient``. They are exercised here by calling ``require_auth`` directly.

  * **The principal return value is never read.** Routes consume
    ``request.state.user_id``, so ``"open"`` / ``"bearer"`` / ``"user:<uid>"``
    are asserted nowhere.

  * **The hash is only ever compared against itself.**
    ``test_helper_stores_only_hash`` asserts ``row["token_hash"] ==
    hash_token(plaintext)`` — equally true under sha1, blake2s, or a latin-1
    encoding. The vectors below are literal.

  * **Row plumbing has a fallback nobody takes.** ``connect()`` installs
    ``sqlite3.Row``, so the positional ``row[1]`` / ``row[2]`` branch the module
    keeps for tuple factories never executes.

  * **The audit logs are contract, not decoration.** The docstrings promise a
    loud warning when the public-bind override is used, and when a request's
    ``user_id`` disagrees with the authenticated one. Neither was asserted.

Env-var names, principal strings, error details, and digests are written as
literals throughout — deriving them from the module's own constants would let a
mutant rename or re-key both sides at once and stay green.
"""

from __future__ import annotations

import inspect
import logging
import pathlib
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.requests import Request

from parallax.migrations import migrate_to_latest
from parallax.server import auth as auth_module
from parallax.server import create_app
from parallax.server.auth import (
    _resolve_multi_user_token,
    assert_safe_to_start,
    auth_configured,
    bind_host_is_safe,
    current_user_id,
    hash_token,
    metrics_auth_required,
    metrics_public_allowed,
    multi_user_mode,
    require_auth,
)
from parallax.sqlite_store import connect, now_iso

_AUTH_LOGGER = "parallax.server.auth"

_AUTH_ENV_VARS = (
    "PARALLAX_TOKEN",
    "PARALLAX_MULTI_USER",
    "PARALLAX_METRICS_PUBLIC",
    "PARALLAX_BIND_HOST",
    "PARALLAX_ALLOW_OPEN_PUBLIC",
)


@pytest.fixture(autouse=True)
def _scrub_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from "no auth configured, loopback bind".

    Each test then states exactly the env it needs. Without this, a flag left
    behind by another suite could satisfy an assertion that this file intends to
    prove from a known-clean starting point.
    """
    for name in _AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def harden_db(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "auth_harden.db"
    boot = connect(p)
    try:
        migrate_to_latest(boot)
    finally:
        boot.close()
    return p


def _mint(db_path: pathlib.Path, *, user_id: str, plaintext: str) -> str:
    """Insert an api_tokens row for ``plaintext``; return the plaintext."""
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_tokens(token_hash, user_id, created_at, "
            "revoked_at, label) VALUES (?, ?, ?, NULL, ?)",
            (hash_token(plaintext), user_id, now_iso(), "harden"),
        )
        conn.commit()
    finally:
        conn.close()
    return plaintext


def _build_app(db_path: pathlib.Path) -> FastAPI:
    def factory() -> sqlite3.Connection:
        return connect(db_path)

    return create_app(db_factory=factory)


def _request(path: str = "/ingest/memory") -> Request:
    """A minimal ASGI request — enough for ``request.url.path`` and state."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
        }
    )


def _creds(scheme: str, credentials: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


def _memory_payload(user_id: str = "alice") -> dict[str, str]:
    return {
        "user_id": user_id,
        "title": "t",
        "summary": "s",
        "vault_path": "v.md",
    }


# ---------------------------------------------------------------------------
# Env-flag parsing — the edges of every switch
# ---------------------------------------------------------------------------


class TestEnvFlagParsing:
    """``auth_configured`` / ``multi_user_mode`` / ``metrics_public_allowed``.

    The pre-existing suite only ever sets these to ``"1"`` or deletes them, so
    both the normalisation (strip + lower) and the closedness of the accepted
    set are unobserved.
    """

    def test_whitespace_only_token_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``PARALLAX_TOKEN="   "`` is a misconfiguration, not a secret.

        Without the ``.strip()`` this reads as "auth configured" — and since
        ``_expected_token()`` strips too, the expected secret becomes ``""``,
        which an empty bearer would then match. Fail-closed means: a token made
        only of whitespace configures nothing.
        """
        monkeypatch.setenv("PARALLAX_TOKEN", "   ")
        assert auth_configured() is False

    def test_empty_token_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Set-but-empty is indistinguishable from unset."""
        monkeypatch.setenv("PARALLAX_TOKEN", "")
        assert auth_configured() is False

    def test_non_empty_token_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_TOKEN", "s3cret")
        assert auth_configured() is True

    def test_multi_user_accepts_uppercase_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The docstring promises case-insensitivity; nothing proved it."""
        monkeypatch.setenv("PARALLAX_MULTI_USER", "TRUE")
        assert multi_user_mode() is True

    def test_multi_user_accepts_padded_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trailing newline from a .env file must not disable tenancy."""
        monkeypatch.setenv("PARALLAX_MULTI_USER", " 1\n")
        assert multi_user_mode() is True

    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "off", "2", "yes", "on", "t", "y"]
    )
    def test_multi_user_rejects_non_canonical_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Only ``1``/``true`` select per-user auth.

        A parser widened to "any non-empty value is on" would flip an operator
        who wrote ``PARALLAX_MULTI_USER=0`` into multi-user mode, where the
        shared token no longer works — an outage. The reverse widening (``yes``
        accepted) is equally wrong: the documented set is closed.
        """
        monkeypatch.setenv("PARALLAX_MULTI_USER", raw)
        assert multi_user_mode() is False

    def test_metrics_public_accepts_uppercase_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", "TRUE")
        assert metrics_public_allowed() is True

    def test_metrics_public_accepts_padded_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", " 1\n")
        assert metrics_public_allowed() is True

    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "off", "2", "yes", "on", "t", "y"]
    )
    def test_metrics_public_rejects_non_canonical_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """``PARALLAX_METRICS_PUBLIC=0`` must not open /metrics.

        This is the fail-open direction: a widened parser turns an explicit
        "no" into an anonymous metrics endpoint leaking ingest cadence and
        shadow discrepancy rates.
        """
        monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", raw)
        assert metrics_public_allowed() is False


# ---------------------------------------------------------------------------
# metrics_auth_required — the multi-user half of the disjunction
# ---------------------------------------------------------------------------


class TestMetricsAuthRequired:
    def test_required_in_multi_user_mode_without_shared_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-user alone is "auth configured" for /metrics purposes.

        The pre-existing metrics cases only ever set ``PARALLAX_TOKEN``, so
        dropping ``multi_user_mode()`` from the disjunction leaves a pure
        multi-user deployment's /metrics anonymous with the suite still green.
        """
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        assert metrics_auth_required() is True

    def test_public_override_applies_in_multi_user_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The override is mode-independent — it is checked first, for both."""
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        monkeypatch.setenv("PARALLAX_METRICS_PUBLIC", "1")
        assert metrics_auth_required() is False

    def test_open_mode_leaves_metrics_open(self) -> None:
        """No token, no multi-user → nothing to enforce."""
        assert metrics_auth_required() is False


# ---------------------------------------------------------------------------
# bind_host_is_safe — an exact allowlist, not a fuzzy match
# ---------------------------------------------------------------------------


class TestBindHostIsSafe:
    @pytest.mark.parametrize(
        "host", [" 127.0.0.1 ", "\tlocalhost\n", " ::1", "[::1] "]
    )
    def test_whitespace_padding_is_stripped(self, host: str) -> None:
        """Env values arrive with stray whitespace; the guard normalises it.

        The pre-existing parametrize list is entirely unpadded, so dropping the
        ``.strip()`` turns a loopback bind into a refusal-to-start at boot.
        """
        assert bind_host_is_safe(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "evil-localhost.example.com",
            "localhost.attacker.example",
            "127.0.0.1.evil.example",
            "[::1].evil.example",
            "2001:db8::1",
        ],
    )
    def test_lookalike_hosts_are_unsafe(self, host: str) -> None:
        """Membership, not substring containment.

        The pre-existing unsafe list ("0.0.0.0", "192.168.1.111", "::",
        "parallax.example.com") shares no substring with the allowlist, so a
        guard relaxed to ``any(h in host ...)`` passes it while waving through
        every public hostname that merely *contains* "localhost" or "::1".
        """
        assert bind_host_is_safe(host) is False

    @pytest.mark.parametrize("host", ["127.0.0.2", "127.1.1.1", "127.255.255.254"])
    def test_other_loopback_range_addresses_are_unsafe(self, host: str) -> None:
        """The allowlist is five exact strings, not the 127.0.0.0/8 block.

        A guard widened to ``host.startswith("127.")`` is invisible to the
        pre-existing list. Pinning the narrow reading keeps the check
        fail-closed: an operator binding to a non-canonical loopback alias is
        told to configure auth rather than silently trusted.
        """
        assert bind_host_is_safe(host) is False


# ---------------------------------------------------------------------------
# assert_safe_to_start — the escape hatch and its audit trail
# ---------------------------------------------------------------------------


class TestAssertSafeToStart:
    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "off", "2", "yes", "on"]
    )
    def test_allow_open_public_rejects_non_canonical_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Only ``1``/``true`` disable the public-bind guard.

        This is the most dangerous widening in the module: with the override
        parsed as "any non-empty value", an operator who wrote
        ``PARALLAX_ALLOW_OPEN_PUBLIC=0`` — explicitly asking to keep the safety
        net — boots an unauthenticated kernel on a public interface.
        """
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("PARALLAX_ALLOW_OPEN_PUBLIC", raw)
        with pytest.raises(RuntimeError, match="refusing to start"):
            assert_safe_to_start()

    def test_allow_open_public_accepts_uppercase_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("PARALLAX_ALLOW_OPEN_PUBLIC", "TRUE")
        assert_safe_to_start()

    def test_allow_open_public_accepts_padded_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("PARALLAX_ALLOW_OPEN_PUBLIC", " 1\n")
        assert_safe_to_start()

    def test_override_emits_audit_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The docstring promises a loud warning; nothing asserted it.

        Deleting the ``_log.warning`` call is invisible to every existing test
        (they only check that no exception is raised), yet it is the single
        signal a post-incident log reader has that the net was switched off.
        """
        monkeypatch.setenv("PARALLAX_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("PARALLAX_ALLOW_OPEN_PUBLIC", "1")
        with caplog.at_level(logging.WARNING, logger=_AUTH_LOGGER):
            assert_safe_to_start()
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == _AUTH_LOGGER and r.levelno >= logging.WARNING
        ]
        assert any("allow_open_public_override" in m for m in warnings), warnings
        assert any("0.0.0.0" in m for m in warnings), warnings

    def test_override_warns_even_on_loopback_bind(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The override branch is checked before the bind host, and says so.

        A mutant that reorders the two — loopback early-return first — is
        behaviourally identical except that the audit line disappears for every
        loopback deployment that has the override set, which is exactly the
        configuration an operator is most likely to forget about.
        """
        monkeypatch.setenv("PARALLAX_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("PARALLAX_ALLOW_OPEN_PUBLIC", "1")
        with caplog.at_level(logging.WARNING, logger=_AUTH_LOGGER):
            assert_safe_to_start()
        assert any(
            "allow_open_public_override" in r.getMessage()
            for r in caplog.records
            if r.name == _AUTH_LOGGER
        )

    def test_refusal_message_names_host_and_escape_hatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal has to be actionable at 3am.

        ``match="refusing to start"`` passes for a message stripped down to
        those three words. The operator needs the offending host and the name
        of the opt-out.
        """
        monkeypatch.setenv("PARALLAX_BIND_HOST", "10.1.2.3")
        with pytest.raises(RuntimeError) as excinfo:
            assert_safe_to_start()
        message = str(excinfo.value)
        assert "10.1.2.3" in message
        assert "PARALLAX_ALLOW_OPEN_PUBLIC" in message
        assert "PARALLAX_TOKEN" in message


# ---------------------------------------------------------------------------
# hash_token — literal vectors, not self-comparison
# ---------------------------------------------------------------------------


class TestHashToken:
    def test_matches_literal_sha256_vector(self) -> None:
        """Pins the algorithm and the lowercase hex form.

        ``test_helper_stores_only_hash`` compares ``hash_token`` to itself, so
        it holds for sha1, blake2s, an uppercase digest, or a salted variant.
        Swapping the digest would also silently invalidate every token already
        stored in a deployed ``api_tokens`` table.
        """
        digest = hash_token("parallax-token-vector")
        assert digest == (
            "e8b1a89a425f650bb12098e224ad9020"
            "41b5397b46ef87a7fc5ef5c8fb6d2840"
        )
        assert len(digest) == 64

    def test_non_ascii_token_is_utf8_encoded(self) -> None:
        """The encoding is part of the contract.

        Every existing token in the suite comes from ``secrets.token_urlsafe``
        and is pure ASCII, where utf-8 and latin-1 agree. A pass-phrase token
        does not: under latin-1 this input hashes to 0867ec9e… instead.
        """
        assert hash_token("pässwörd") == (
            "46970bef70aced8123f0d5d094717e2a"
            "5cd412041e03b26376049fe65b2834a4"
        )


# ---------------------------------------------------------------------------
# require_auth — single-token mode
# ---------------------------------------------------------------------------


class TestSingleTokenMode:
    def test_padded_env_token_authenticates_unpadded_bearer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_expected_token()`` strips, so a padded env value still works.

        Deployment files routinely leave a trailing newline on a secret. Both
        sides are stripped; the pre-existing tokens ("secret", "t0ken") are
        unpadded, so the strip is unobserved.
        """
        monkeypatch.setenv("PARALLAX_TOKEN", "  s3cret\n")
        principal = require_auth(_request(), _creds("Bearer", "s3cret"), None)
        assert principal == "bearer"

    def test_non_bearer_scheme_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``HTTPBearer`` normally filters this, so the module's own check is
        never reached through ``TestClient`` — it is defence in depth for any
        caller that supplies credentials directly, and it must hold."""
        monkeypatch.setenv("PARALLAX_TOKEN", "s3cret")
        with pytest.raises(HTTPException) as excinfo:
            require_auth(_request(), _creds("Basic", "s3cret"), None)
        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "missing bearer token"

    def test_wrong_token_detail_is_invalid_not_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A supplied-but-wrong token is "invalid"; absence is "missing"."""
        monkeypatch.setenv("PARALLAX_TOKEN", "s3cret")
        with pytest.raises(HTTPException) as excinfo:
            require_auth(_request(), _creds("Bearer", "wrong"), None)
        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "invalid bearer token"
        assert excinfo.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_open_mode_principal_is_literal_open(self) -> None:
        """Nothing in the suite reads the principal; pin all three literals."""
        assert require_auth(_request(), None, None) == "open"

    def test_constant_time_comparison_is_used(self) -> None:
        """The shared-secret compare must not short-circuit on first mismatch.

        ``==`` and ``hmac.compare_digest`` accept exactly the same token set, so
        no black-box assertion can separate them — the difference is wall-clock
        timing, which a byte-at-a-time attacker can use to recover the secret.
        Asserting on the source is the only way to keep this decision from
        being quietly refactored away.
        """
        source = inspect.getsource(auth_module.require_auth)
        assert "hmac.compare_digest" in source


# ---------------------------------------------------------------------------
# require_auth — mode precedence
# ---------------------------------------------------------------------------


class TestModePrecedence:
    def test_multi_user_wins_when_shared_token_also_set(
        self, harden_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With both flags set, the shared secret must NOT be a master key.

        ``mu_app`` deletes ``PARALLAX_TOKEN`` and ``auth_app`` deletes
        ``PARALLAX_MULTI_USER``, so the two modes are never on together and
        their order is unpinned. A mutant that checks ``auth_configured()``
        first turns the operator's leftover ``PARALLAX_TOKEN`` into a
        credential that authenticates against *every* tenant — and, because it
        never sets ``request.state.user_id``, one that then reads whatever
        ``?user_id=`` the caller asks for.
        """
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        monkeypatch.setenv("PARALLAX_TOKEN", "shared-secret")
        monkeypatch.setenv("PARALLAX_DB_PATH", str(harden_db))
        alice_bearer = _mint(harden_db, user_id="alice", plaintext="alice-token")

        with TestClient(_build_app(harden_db)) as client:
            shared = client.post(
                "/ingest/memory",
                json=_memory_payload(),
                headers={"Authorization": "Bearer shared-secret"},
            )
            per_user = client.post(
                "/ingest/memory",
                json=_memory_payload(),
                headers={"Authorization": f"Bearer {alice_bearer}"},
            )
        assert shared.status_code == 401, shared.text
        assert per_user.status_code == 201, per_user.text
        assert per_user.json()["user_id"] == "alice"


# ---------------------------------------------------------------------------
# require_auth — multi-user credential guards
# ---------------------------------------------------------------------------


class TestMultiUserCredentialGuards:
    def test_non_bearer_scheme_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        with pytest.raises(HTTPException) as excinfo:
            require_auth(_request(), _creds("Basic", "anything"), None)
        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "missing bearer token"

    def test_whitespace_only_credential_reports_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"Bearer    " is a *missing* token, and must never reach the DB.

        ``get_authorization_scheme_param`` hands the module a truthy ``"   "``,
        so without the strip-then-empty guard this becomes a normal lookup:
        sha256 of whitespace, a table hit that can never match, and an
        "invalid bearer token" that misdirects whoever is reading the logs.
        Passing ``conn=None`` proves the guard fires before any query — a
        mutant that drops it raises AttributeError instead.
        """
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        with pytest.raises(HTTPException) as excinfo:
            require_auth(_request(), _creds("Bearer", "   "), None)
        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "missing bearer token"

    def test_principal_is_literal_user_prefix(
        self, harden_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``"user:<uid>"`` — the prefix distinguishes it from ``"bearer"``."""
        monkeypatch.setenv("PARALLAX_MULTI_USER", "1")
        _mint(harden_db, user_id="alice", plaintext="alice-token")
        conn = connect(harden_db)
        try:
            principal = require_auth(
                _request(), _creds("Bearer", "alice-token"), conn
            )
        finally:
            conn.close()
        assert principal == "user:alice"


# ---------------------------------------------------------------------------
# _resolve_multi_user_token — revocation, hashing, row plumbing
# ---------------------------------------------------------------------------


class TestTokenRowResolution:
    def test_empty_string_revoked_at_still_rejects(
        self, harden_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any non-NULL ``revoked_at`` means revoked.

        The existing revocation test stamps ``now_iso()``, a truthy string, so
        relaxing ``is not None`` to a truthiness check stays green — while a
        row revoked by a tool that writes an empty marker silently works again.
        """
        _mint(harden_db, user_id="alice", plaintext="alice-token")
        conn = connect(harden_db)
        try:
            conn.execute(
                "UPDATE api_tokens SET revoked_at = '' WHERE token_hash = ?",
                (hash_token("alice-token"),),
            )
            conn.commit()
            with pytest.raises(HTTPException) as excinfo:
                _resolve_multi_user_token(_request(), "alice-token", conn)
        finally:
            conn.close()
        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == "invalid bearer token"

    def test_stored_hash_is_not_itself_a_bearer_token(
        self, harden_db: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leaked ``api_tokens`` dump must not be a set of live credentials.

        That is the whole point of storing a hash. The supplied token is hashed
        *before* the lookup, so presenting the stored digest yields
        sha256(digest) — a miss. Nothing in the suite ever tried it, so a
        lookup relaxed to match either column value survives.
        """
        _mint(harden_db, user_id="alice", plaintext="alice-token")
        stored_hash = hash_token("alice-token")
        conn = connect(harden_db)
        try:
            with pytest.raises(HTTPException) as excinfo:
                _resolve_multi_user_token(_request(), stored_hash, conn)
        finally:
            conn.close()
        assert excinfo.value.status_code == 401

    def test_tuple_row_factory_resolves_user_id(
        self, harden_db: pathlib.Path
    ) -> None:
        """The positional fallback the module documents actually works.

        ``connect()`` installs ``sqlite3.Row``, so ``row[1]`` / ``row[2]`` never
        execute in the suite and the two indices could be swapped — or point at
        ``token_hash`` — without a single test noticing.
        """
        _mint(harden_db, user_id="alice", plaintext="alice-token")
        conn = sqlite3.connect(str(harden_db))  # default tuple row factory
        try:
            request = _request()
            user_id = _resolve_multi_user_token(request, "alice-token", conn)
        finally:
            conn.close()
        assert user_id == "alice"
        assert request.state.user_id == "alice"

    def test_tuple_row_factory_still_honours_revocation(
        self, harden_db: pathlib.Path
    ) -> None:
        """Same fallback, revocation half: ``row[2]`` must be ``revoked_at``."""
        _mint(harden_db, user_id="alice", plaintext="alice-token")
        conn = sqlite3.connect(str(harden_db))
        try:
            conn.execute(
                "UPDATE api_tokens SET revoked_at = ? WHERE token_hash = ?",
                ("2026-08-23T00:00:00Z", hash_token("alice-token")),
            )
            conn.commit()
            with pytest.raises(HTTPException) as excinfo:
                _resolve_multi_user_token(_request(), "alice-token", conn)
        finally:
            conn.close()
        assert excinfo.value.status_code == 401

    def test_non_text_user_id_is_coerced_to_str(self, tmp_path: pathlib.Path) -> None:
        """``request.state.user_id`` is always a ``str``.

        SQLite is dynamically typed: a column declared TEXT applies affinity,
        but a table without declared types (as built here) hands back the int
        verbatim. Downstream scoping compares ``user_id`` against TEXT columns,
        where ``42`` and ``"42"`` are different keys — so the coercion is load
        bearing, and nothing in the suite observes it.
        """
        conn = sqlite3.connect(str(tmp_path / "untyped.db"))
        try:
            conn.execute(
                "CREATE TABLE api_tokens "
                "(token_hash, user_id, created_at, revoked_at, label)"
            )
            conn.execute(
                "INSERT INTO api_tokens VALUES (?, 42, '2026-08-23', NULL, 'x')",
                (hash_token("numeric-token"),),
            )
            conn.commit()
            request = _request()
            user_id = _resolve_multi_user_token(request, "numeric-token", conn)
        finally:
            conn.close()
        assert user_id == "42"
        assert isinstance(request.state.user_id, str)


# ---------------------------------------------------------------------------
# current_user_id — the tenant-selection funnel
# ---------------------------------------------------------------------------


class TestCurrentUserId:
    def test_empty_authed_user_falls_back_to_request_value(self) -> None:
        """An empty ``request.state.user_id`` is absence, not an identity.

        Relaxing ``if authed:`` to ``if authed is not None:`` makes an empty
        string win over the caller's value, scoping the request to the ``""``
        namespace — reads return nothing and writes land somewhere nobody can
        read back.
        """
        request = _request()
        request.state.user_id = ""
        assert current_user_id(request, "bob") == "bob"

    def test_authed_user_beats_request_supplied_value(self) -> None:
        request = _request()
        request.state.user_id = "alice"
        assert current_user_id(request, "carol") == "alice"

    def test_disagreeing_request_user_id_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The spoof attempt is the interesting security event.

        The existing tests prove the *result* (Alice's namespace wins) but not
        that anything was recorded, so deleting the warning is free.
        """
        request = _request()
        request.state.user_id = "alice"
        with caplog.at_level(logging.WARNING, logger=_AUTH_LOGGER):
            assert current_user_id(request, "carol") == "alice"
        messages = [
            r.getMessage() for r in caplog.records if r.name == _AUTH_LOGGER
        ]
        assert any("auth.user_id.override" in m for m in messages), messages
        assert any("carol" in m for m in messages), messages

    def test_matching_request_user_id_is_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pins the ``!=``: agreement is the normal case, not an incident.

        A mutant that inverts the comparison warns on every well-behaved
        request and stays silent on the spoof — the exact inverse of the
        intended signal, and invisible to a test that only checks the return.
        """
        request = _request()
        request.state.user_id = "alice"
        with caplog.at_level(logging.WARNING, logger=_AUTH_LOGGER):
            assert current_user_id(request, "alice") == "alice"
        assert [r for r in caplog.records if r.name == _AUTH_LOGGER] == []

    def test_missing_user_id_raises_400(self) -> None:
        """Neither authenticated nor supplied → a client error, not a 401/500.

        401 would tell an authenticated caller to re-authenticate for what is
        actually a malformed request.
        """
        with pytest.raises(HTTPException) as excinfo:
            current_user_id(_request(), None)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "user_id required"

    def test_empty_fallback_also_raises_400(self) -> None:
        """``?user_id=`` is absence too, not a tenant named ``""``."""
        with pytest.raises(HTTPException) as excinfo:
            current_user_id(_request(), "")
        assert excinfo.value.status_code == 400

    def test_non_str_authed_user_is_coerced(self) -> None:
        """``current_user_id`` promises ``str``; pin the coercion here too."""
        request = _request()
        request.state.user_id = 42
        result = current_user_id(request, None)
        assert result == "42"
        assert isinstance(result, str)
