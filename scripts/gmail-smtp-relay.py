#!/usr/bin/env python3
"""Alertmanager -> Gmail SMTP relay.

Listens on RELAY_HOST:RELAY_PORT (default 127.0.0.1:9096), accepts
Alertmanager webhook payload, renders a plain-text email per alert
group, and forwards via Gmail SMTP (smtp.gmail.com:587 STARTTLS).

Why a relay and not Alertmanager's built-in ``email_configs``?
Alertmanager email auth flow requires storing the App Password in the
``alertmanager.yml`` file (the file is bind-mounted into the container).
Routing through a host-local relay lets us keep the secret in
``/etc/parallax/gmail-smtp-relay.env`` (chmod 600, root-owned), mirrors
the Discord relay topology, and gives us one consistent journald log
surface for both notification paths.

Auth: Gmail App Password (Chris has 2FA + App Password set up). Standard
username + password over SMTP-AUTH after STARTTLS. **Never** commit the
password — see ``scripts/gmail-smtp-relay.env.example``.

Failure mode: SMTP timeout / auth fail / send rejection -> log WARNING and
return HTTP 500 so Alertmanager retries per its webhook backoff. Auth
failures will retry until the operator rotates the App Password; the
``repeat_interval: 4h`` in alertmanager.yml caps how fast that happens.

Stdlib only -- no pip deps.
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Subject + body bounds — keep emails small for inbox readability. Gmail
# accepts much larger but a 100-alert blast is almost never useful in a
# single message body.
SUBJECT_MAX = 200
BODY_PREVIEW_ALERTS = 25
LABEL_VALUE_TRIM = 200
ANNOTATION_TRIM = 600

# Maximum JSON payload we accept on a single POST. Alertmanager group
# payloads are kilobytes; this cap guards against a buggy or malicious
# local caller declaring a huge Content-Length and pinning memory.
MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

# Labels surfaced in the per-alert detail block. Anything else is omitted
# to keep the body scannable.
SURFACED_LABELS: tuple[str, ...] = (
    "severity",
    "rollback_class",
    "trigger",
    "instance",
    "job",
)


@dataclass(frozen=True)
class RelayConfig:
    """Resolved runtime configuration. All required fields must be present."""

    smtp_user: str
    smtp_app_password: str
    smtp_to: str
    smtp_host: str
    smtp_port: int
    smtp_timeout_sec: float
    listen_host: str
    listen_port: int


def _require_env(name: str) -> str:
    """Return the env var, treating absent OR empty-string as missing.

    Empty-string-as-missing is intentional: ``KEY=`` in the env file is
    the sentinel "you forgot to fill this in", and a silent fall-through
    would result in invalid SMTP credentials at runtime.
    """
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"FATAL: {name} env not set")
    return value


def _assert_header_safe(name: str, value: str) -> None:
    """Reject CR/LF in values destined for RFC 5322 headers."""
    if "\r" in value or "\n" in value:
        raise SystemExit(f"FATAL: {name} env contains CR/LF — refusing to start")


def load_config() -> RelayConfig:
    """Read configuration from the process environment. Exits on missing required vars."""
    smtp_user = _require_env("GMAIL_SMTP_USER")
    smtp_to = _require_env("GMAIL_SMTP_TO")
    _assert_header_safe("GMAIL_SMTP_USER", smtp_user)
    _assert_header_safe("GMAIL_SMTP_TO", smtp_to)
    return RelayConfig(
        smtp_user=smtp_user,
        smtp_app_password=_require_env("GMAIL_SMTP_APP_PASSWORD"),
        smtp_to=smtp_to,
        smtp_host=os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.environ.get("GMAIL_SMTP_PORT", "587")),
        smtp_timeout_sec=float(os.environ.get("GMAIL_SMTP_TIMEOUT", "10")),
        listen_host=os.environ.get("RELAY_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("RELAY_PORT", "9096")),
    )


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict; otherwise an empty dict.

    Defensive against malformed payloads (e.g. ``commonLabels: []``) that
    would otherwise crash ``.get()`` with ``AttributeError`` and leak a
    500 to alertmanager for what is really a 400-class condition.
    """
    return value if isinstance(value, dict) else {}


def _format_subject(payload: dict[str, Any]) -> str:
    common_labels = _as_dict(payload.get("commonLabels"))
    common_annotations = _as_dict(payload.get("commonAnnotations"))
    status = str(payload.get("status", "firing")).upper()
    severity = str(common_labels.get("severity", "info"))
    alertname = str(common_labels.get("alertname", "unknown"))
    summary = str(common_annotations.get("summary", "") or "")
    base = f"[{status}][{severity}] {alertname}"
    if summary:
        base = f"{base} - {summary}"
    return _trim(base, SUBJECT_MAX)


def _format_alert_block(alert: dict[str, Any]) -> str:
    labels = _as_dict(alert.get("labels"))
    ann = _as_dict(alert.get("annotations"))
    status = str(alert.get("status", "firing"))
    starts_at = str(alert.get("startsAt", ""))
    ends_at = str(alert.get("endsAt", ""))
    generator_url = str(alert.get("generatorURL", ""))

    lines: list[str] = []
    lines.append(f"- status     : {status}")
    for key in SURFACED_LABELS:
        if key in labels:
            lines.append(f"  {key:<10}: {_trim(str(labels[key]), LABEL_VALUE_TRIM)}")
    summary = ann.get("summary")
    description = ann.get("description")
    if summary:
        lines.append(f"  summary   : {_trim(str(summary), ANNOTATION_TRIM)}")
    if description and description != summary:
        lines.append(f"  description: {_trim(str(description), ANNOTATION_TRIM)}")
    if starts_at:
        lines.append(f"  startsAt  : {starts_at}")
    if status == "resolved" and ends_at:
        lines.append(f"  endsAt    : {ends_at}")
    if generator_url:
        lines.append(f"  source    : {generator_url}")
    return "\n".join(lines)


def render_email(payload: dict[str, Any]) -> tuple[str, str]:
    """Render an alertmanager payload to (subject, body)."""
    subject = _format_subject(payload)
    status = str(payload.get("status", "firing")).upper()
    raw_alerts = payload.get("alerts")
    alerts: list[dict[str, Any]] = list(raw_alerts) if isinstance(raw_alerts, list) else []
    common_labels = _as_dict(payload.get("commonLabels"))
    external_url = str(payload.get("externalURL", ""))

    header_lines = [
        f"Alertmanager group status: {status}",
        f"Group key: {payload.get('groupKey', '')}",
        f"Alert count: {len(alerts)}",
    ]
    if common_labels:
        header_lines.append("Common labels:")
        for k, v in sorted(common_labels.items()):
            header_lines.append(f"  {k}={_trim(str(v), LABEL_VALUE_TRIM)}")
    if external_url:
        header_lines.append(f"Alertmanager: {external_url}")

    body_parts = ["\n".join(header_lines), ""]
    visible = alerts[:BODY_PREVIEW_ALERTS]
    for idx, alert in enumerate(visible, 1):
        body_parts.append(f"Alert {idx}/{len(alerts)}:")
        body_parts.append(_format_alert_block(_as_dict(alert)))
        body_parts.append("")
    if len(alerts) > BODY_PREVIEW_ALERTS:
        omitted = len(alerts) - BODY_PREVIEW_ALERTS
        body_parts.append(f"... and {omitted} more alert(s) suppressed from this email.")

    return subject, "\n".join(body_parts).rstrip() + "\n"


def build_message(cfg: RelayConfig, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.smtp_user
    msg["To"] = cfg.smtp_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="parallax-alertmanager-relay")
    msg["X-Mailer"] = "parallax-alertmanager-gmail-smtp-relay/1.0"
    msg.set_content(body)
    return msg


def send_email(cfg: RelayConfig, msg: EmailMessage) -> dict[str, tuple[int, bytes]]:
    """Send via Gmail SMTP STARTTLS. Raises on any SMTP-level failure.

    Returns the dict of partially-refused recipients (empty when all
    recipients were accepted). Callers should treat a non-empty dict as
    a delivery failure for retry purposes.
    """
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout_sec) as smtp:
        smtp.ehlo()
        smtp.starttls()
        # Second EHLO is required by RFC 3207 so the server re-advertises
        # its capabilities (notably AUTH) over the now-encrypted channel.
        smtp.ehlo()
        smtp.login(cfg.smtp_user, cfg.smtp_app_password)
        refused = smtp.send_message(msg)
        return refused or {}


def make_handler(cfg: RelayConfig) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class closed over ``cfg``.

    HTTPServer instantiates the handler class per-request, so configuration is
    threaded through a closure rather than constructor injection (which the
    BaseHTTPRequestHandler API does not support cleanly).
    """

    class Handler(BaseHTTPRequestHandler):
        """Receives Alertmanager webhooks, fans them out to Gmail as plain-text email."""

        def _reply(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            raw_len = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_len)
            except ValueError:
                print(
                    f"bad Content-Length from {self.client_address}: {raw_len!r}",
                    file=sys.stderr,
                )
                self._reply(400, b"bad Content-Length")
                return
            if length < 0:
                self._reply(400, b"bad Content-Length")
                return
            if length > MAX_BODY_BYTES:
                print(
                    f"oversized payload from {self.client_address}: "
                    f"{length} > {MAX_BODY_BYTES} bytes",
                    file=sys.stderr,
                )
                self._reply(413, b"payload too large")
                return
            if length == 0:
                print(
                    f"empty payload from {self.client_address}; refusing to send blank email",
                    file=sys.stderr,
                )
                self._reply(400, b"empty payload")
                return
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                print(
                    f"bad json from {self.client_address}: {exc}",
                    file=sys.stderr,
                )
                self._reply(400, f"bad json: {exc}".encode())
                return
            if not isinstance(body, dict):
                self._reply(400, b"bad payload: expected JSON object")
                return
            subject, text = render_email(body)
            msg = build_message(cfg, subject, text)
            try:
                refused = send_email(cfg, msg)
            except (smtplib.SMTPException, OSError) as exc:
                # SMTP auth fail, timeout, DNS, TLS, send rejection — all
                # map to 500 so Alertmanager keeps the alert in its retry
                # queue.
                print(
                    f"gmail-smtp send failure: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                self._reply(500, f"smtp send failed: {type(exc).__name__}".encode())
                return
            if refused:
                # Partial delivery — Gmail accepted the envelope but refused
                # one or more RCPT entries. Treat as failure so alertmanager
                # retries; the operator should inspect GMAIL_SMTP_TO.
                print(
                    f"gmail-smtp partial delivery; refused={list(refused)}",
                    file=sys.stderr,
                )
                self._reply(500, b"smtp partial delivery")
                return
            self._reply(204)

        def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
            if self.path == "/healthz":
                self._reply(200, b"ok")
            else:
                self._reply(404)

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 — base override
            sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    return Handler


def main() -> None:
    cfg = load_config()
    handler_cls = make_handler(cfg)
    # ThreadingHTTPServer so a slow Gmail send does not head-of-line block
    # subsequent webhook POSTs. Each request gets its own SMTP connection.
    srv = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), handler_cls)
    print(f"gmail-smtp-relay listening on {cfg.listen_host}:{cfg.listen_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
