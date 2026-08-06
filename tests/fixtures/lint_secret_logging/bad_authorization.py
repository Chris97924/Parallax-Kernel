"""NEGATIVE fixture for PLX-SECRET-LOG — every call below must be flagged.

These are the shapes ``m5-entry-spec.md`` §3.1a forbids: the raw Authorization
value reaching ``logger.*()`` or ``audit_log.write()``. Each is a route the
value could take, including the indirect ones (f-string, dict, ``str()``,
``%``-args), because a secret does not stop being a secret when interpolated.

Not collected by pytest (no ``test_`` prefix); driven from
``tests/scripts/test_lint_no_raw_secret_logging.py``.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)
logger = logging.getLogger("other")


class _AuditLog:
    def write(self, **fields: object) -> None: ...


audit_log = _AuditLog()


class _Client:
    def __init__(self, token: str) -> None:
        self._token = token
        self.headers = {"Authorization": f"Bearer {token}"}

    def emit(self) -> None:
        # 1 — attribute holding the credential, positional.
        _log.warning("aphelion.auth", self._token)
        # 2 — subscript into the header map.
        _log.error("aphelion.auth", extra={"h": self.headers["Authorization"]})


def leak_direct(authorization: str) -> None:
    # 3 — bare name, keyword argument.
    _log.info("aphelion.request", extra={"authorization": authorization})


def leak_fstring(bearer_token: str) -> None:
    # 4 — interpolated into the message.
    logger.warning(f"calling aphelion with {bearer_token}")


def leak_str_wrapper(api_key: str) -> None:
    # 5 — wrapped in str(), the shape that reads as "sanitised" but is not.
    _log.debug("aphelion.key", extra={"k": str(api_key)})


def leak_printf(password: str) -> None:
    # 6 — %-formatting args.
    _log.error("login failed for %s", password)


def leak_audit_write(auth_header: str) -> None:
    # 7 — the audit-ledger half of the spec rule.
    audit_log.write(event="aphelion.read", header=auth_header)


def leak_nested(secret_value: str) -> None:
    # 8 — buried in a nested literal.
    _log.warning("cfg", extra={"outer": {"inner": [secret_value]}})
