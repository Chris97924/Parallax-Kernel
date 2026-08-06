"""POSITIVE fixture for PLX-SECRET-LOG — must lint clean.

Everything here is a shape the rule has to permit, or the gate would push
authors toward rewording their logs instead of redacting their values.

Not collected by pytest (no ``test_`` prefix); driven from
``tests/scripts/test_lint_no_raw_secret_logging.py``.
"""

from __future__ import annotations

import hashlib
import logging

_log = logging.getLogger(__name__)


class _AuditLog:
    def write(self, **fields: object) -> None: ...


audit_log = _AuditLog()


def emit_redacted(token: str, headers: dict[str, str], url: str) -> None:
    # The mitigation the spec asks for: send the placeholder, not the value.
    _log.info("aphelion.request", extra={"authorization": "<REDACTED>"})

    # Derived, non-reversible values are the encouraged alternative.
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:12]
    _log.warning("aphelion.auth_failed", extra={"token_hash": token_hash})
    audit_log.write(event="aphelion.read", api_key_fingerprint=token_hash)

    # Presence / shape metadata carries no secret.
    _log.info("aphelion.config", extra={"token_present": bool(token), "token_len": len(token)})
    _log.debug("aphelion.env", extra={"token_env_name": "PARALLAX_APHELION_TOKEN"})

    # Truthiness and comparison contexts emit the branch or the bool, never
    # the value — the shape ``scripts/burn-in-synth-loader.py`` already uses.
    _log.info("aphelion.auth", extra={"auth": "bearer" if token else "none"})
    _log.debug("aphelion.auth", extra={"configured": token != "", "missing": not token})

    # A credential-*labelled* slot is legal when what fills it is bounded. The
    # label says "a secret belongs here"; these say "and it was handled".
    _log.info("aphelion.request", extra={"Authorization": "<REDACTED>"})
    _log.info("aphelion.request", extra={"authorization": token_hash})
    _log.debug("aphelion.request", extra={"bearer_token": bool(token)})
    audit_log.write(event="aphelion.read", authorization="<REDACTED>")

    # Named mappings are followed now, so the bounded forms have to stay legal
    # there too — otherwise following the binding would just relocate the
    # false positives instead of finding real leaks.
    safe_extra = {"Authorization": "<REDACTED>", "token_present": bool(token)}
    _log.info("aphelion.request", extra=safe_extra)
    derived_extra = {"authorization": token_hash}
    audit_log.write(event="aphelion.read", **derived_extra)

    # Non-credential values are untouched by the rule.
    _log.info("aphelion.request", extra={"url": url, "header_count": len(headers)})

    # Prose that merely mentions a bearer token is not a leak.
    _log.warning("metrics is reachable without a bearer token; gate it upstream")


def reviewed_false_positive(csrf_token: str) -> None:
    # A reviewed exception stays visible in the diff rather than silently
    # weakening the rule for everyone.
    _log.info("form.issued", extra={"csrf": csrf_token})  # plx-allow: PLX-SECRET-LOG
