"""Tests for ``scripts/gmail-smtp-relay.py``.

The relay script is loaded via importlib because it lives outside the
``parallax`` package and its filename uses hyphens. Pattern matches
``tests/scripts/test_burn_in_synth_loader.py``.

Coverage targets (≥95% per spec):
- happy path POST -> SMTP send -> 204 No Content
- SMTP auth fail / timeout / generic SMTPException -> HTTP 500 (alertmanager retry)
- partial recipient refusal -> HTTP 500 (alertmanager retry, not silent success)
- bad / oversized / malformed-Content-Length / empty payloads -> 4xx (no retry)
- payload renderer over the documented Alertmanager schema, including
  defensive paths for non-dict commonLabels and non-list alerts
- subject + label + annotation trimming
- /healthz GET -> 200, unknown GET -> 404
- load_config success / missing env / CRLF-poisoned env -> SystemExit
"""

from __future__ import annotations

import importlib.util
import io
import json
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gmail-smtp-relay.py"
_MODULE_NAME = "_gmail_smtp_relay"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass-with-``from __future__ import annotations``
    # can resolve forward-referenced annotations via the module's __dict__
    # (dataclasses.py looks the module up in sys.modules).
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


@pytest.fixture()
def relay() -> ModuleType:
    return _load_module()


@pytest.fixture()
def cfg(relay: ModuleType) -> Any:
    return relay.RelayConfig(
        smtp_user="alerts@example.com",
        smtp_app_password="abcdefghijklmnop",
        smtp_to="oncall@example.com",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_timeout_sec=5.0,
        listen_host="127.0.0.1",
        listen_port=9096,
    )


# ---------------------------------------------------------------------------
# Sample payloads modeled on the documented Alertmanager v4 webhook schema.
# Reference: https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
# ---------------------------------------------------------------------------


def _firing_payload(alert_count: int = 1, severity: str = "critical") -> dict[str, Any]:
    base_alert = {
        "status": "firing",
        "labels": {
            "alertname": "AphelionQ8Drain",
            "severity": severity,
            "rollback_class": "auto",
            "trigger": "T5",
            "instance": "parallax-zenbook:8000",
            "job": "parallax",
            "extra_unsurfaced": "ignored-in-body",
        },
        "annotations": {
            "summary": "Q8 drain exceeded budget",
            "description": "drain p99 > 250ms for 5m",
        },
        "startsAt": "2026-05-19T01:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "http://prometheus.local/graph?g0=...",
        "fingerprint": "deadbeef0001",
    }
    alerts = []
    for i in range(alert_count):
        alert = dict(base_alert)
        alert["labels"] = dict(base_alert["labels"])
        alert["labels"]["instance"] = f"parallax-zenbook:{8000 + i}"
        alert["fingerprint"] = f"deadbeef{i:04x}"
        alerts.append(alert)
    return {
        "version": "4",
        "groupKey": '{}:{alertname="AphelionQ8Drain"}',
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "gmail-smtp",
        "groupLabels": {"alertname": "AphelionQ8Drain"},
        "commonLabels": {"alertname": "AphelionQ8Drain", "severity": severity},
        "commonAnnotations": {"summary": "Q8 drain exceeded budget"},
        "externalURL": "http://alertmanager.local:9093",
        "alerts": alerts,
    }


def _resolved_payload() -> dict[str, Any]:
    payload = _firing_payload(alert_count=1, severity="warning")
    payload["status"] = "resolved"
    payload["alerts"][0]["status"] = "resolved"
    payload["alerts"][0]["endsAt"] = "2026-05-19T01:30:00Z"
    return payload


# ---------------------------------------------------------------------------
# Subject / body rendering
# ---------------------------------------------------------------------------


def test_render_email_subject_uses_status_severity_alertname_summary(
    relay: ModuleType,
) -> None:
    payload = _firing_payload(severity="critical")
    subject, _ = relay.render_email(payload)
    assert subject.startswith("[FIRING][critical] AphelionQ8Drain")
    assert "Q8 drain exceeded budget" in subject


def test_render_email_subject_handles_missing_common_labels(relay: ModuleType) -> None:
    subject, _ = relay.render_email({"status": "firing", "alerts": []})
    # default severity=info, alertname=unknown when commonLabels absent
    assert subject == "[FIRING][info] unknown"


def test_render_email_subject_trimmed_to_max(relay: ModuleType) -> None:
    payload = _firing_payload()
    payload["commonAnnotations"]["summary"] = "x" * 1000
    subject, _ = relay.render_email(payload)
    assert len(subject) <= relay.SUBJECT_MAX


def test_render_email_subject_strips_crlf_from_summary(relay: ModuleType) -> None:
    """Newlines in commonAnnotations.summary must be collapsed; otherwise
    ``EmailMessage["Subject"] = subject`` raises ValueError in stdlib."""
    payload = _firing_payload()
    payload["commonAnnotations"]["summary"] = "line one\nline two\r\nline three\tcol"
    subject, _ = relay.render_email(payload)
    assert "\n" not in subject
    assert "\r" not in subject
    assert "\t" not in subject
    assert "line one line two line three col" in subject


def test_render_email_subject_strips_crlf_from_alertname_and_severity(
    relay: ModuleType,
) -> None:
    payload = _firing_payload()
    payload["commonLabels"]["alertname"] = "weird\rname"
    payload["commonLabels"]["severity"] = "crit\nical"
    subject, _ = relay.render_email(payload)
    assert "\n" not in subject
    assert "\r" not in subject


def test_build_message_accepts_subject_built_from_multiline_payload(
    relay: ModuleType, cfg: Any
) -> None:
    """Round-trip: the subject produced by render_email must always be
    assignable to ``EmailMessage["Subject"]`` without ValueError, regardless
    of how badly the upstream payload is formatted."""
    payload = _firing_payload()
    payload["commonAnnotations"]["summary"] = "burst\nof\nnewlines\rwith CR"
    subject, body = relay.render_email(payload)
    # If render_email did its job, this assignment does not raise.
    msg = relay.build_message(cfg, subject, body)
    assert "\n" not in msg["Subject"]
    assert "\r" not in msg["Subject"]


def test_render_email_body_lists_each_alert_and_common_labels(relay: ModuleType) -> None:
    payload = _firing_payload(alert_count=3, severity="warning")
    _, body = relay.render_email(payload)
    assert "Alertmanager group status: FIRING" in body
    assert "Alert count: 3" in body
    assert "Common labels:" in body
    assert "alertname=AphelionQ8Drain" in body
    assert "severity=warning" in body
    # Each alert appears as a numbered block
    for i in (1, 2, 3):
        assert f"Alert {i}/3:" in body
    # Surfaced labels present
    assert "severity   :" in body or "severity  :" in body
    assert "trigger    :" in body or "trigger   :" in body
    # Annotations
    assert "summary" in body
    assert "description" in body
    # generatorURL surfaced
    assert "source    : http://prometheus.local/graph?g0=..." in body


def test_render_email_body_omits_unsurfaced_labels(relay: ModuleType) -> None:
    _, body = relay.render_email(_firing_payload())
    assert "extra_unsurfaced" not in body


def test_render_email_body_truncates_above_preview_cap(relay: ModuleType) -> None:
    payload = _firing_payload(alert_count=relay.BODY_PREVIEW_ALERTS + 5)
    _, body = relay.render_email(payload)
    assert "... and 5 more alert(s) suppressed from this email." in body
    # Last shown should be the BODY_PREVIEW_ALERTS-th
    assert f"Alert {relay.BODY_PREVIEW_ALERTS}/{relay.BODY_PREVIEW_ALERTS + 5}:" in body
    # The (BODY_PREVIEW_ALERTS+1)-th must not appear
    assert f"Alert {relay.BODY_PREVIEW_ALERTS + 1}/" not in body


def test_render_email_resolved_payload_marks_status_and_ends_at(relay: ModuleType) -> None:
    payload = _resolved_payload()
    subject, body = relay.render_email(payload)
    assert subject.startswith("[RESOLVED][warning]")
    assert "status     : resolved" in body
    assert "endsAt    : 2026-05-19T01:30:00Z" in body


def test_render_email_trims_long_label_and_annotation_values(relay: ModuleType) -> None:
    payload = _firing_payload()
    payload["alerts"][0]["labels"]["instance"] = "i" * 5000
    payload["alerts"][0]["annotations"]["summary"] = "s" * 5000
    payload["alerts"][0]["annotations"]["description"] = "d" * 5000
    _, body = relay.render_email(payload)
    # ellipsis present for each trimmed field
    assert body.count("…") >= 3
    # No raw 5000-char strings
    assert "i" * 5000 not in body
    assert "s" * 5000 not in body
    assert "d" * 5000 not in body


def test_render_email_skips_description_when_equal_to_summary(relay: ModuleType) -> None:
    payload = _firing_payload()
    payload["alerts"][0]["annotations"]["description"] = payload["alerts"][0]["annotations"][
        "summary"
    ]
    _, body = relay.render_email(payload)
    assert body.count("summary") >= 1
    # description line should not appear since it duplicates summary
    assert "description:" not in body


def test_render_email_handles_alert_missing_optional_fields(relay: ModuleType) -> None:
    payload = {
        "status": "firing",
        "alerts": [{"status": "firing", "labels": {}, "annotations": {}}],
    }
    subject, body = relay.render_email(payload)
    assert subject == "[FIRING][info] unknown"
    assert "Alert 1/1:" in body
    assert "status     : firing" in body


def test_render_email_handles_non_dict_common_labels(relay: ModuleType) -> None:
    """A malformed payload with commonLabels set to a list must not crash."""
    payload = {
        "status": "firing",
        "commonLabels": ["this", "is", "not", "a", "dict"],
        "alerts": [{"status": "firing", "labels": [], "annotations": None}],
    }
    subject, body = relay.render_email(payload)
    assert subject == "[FIRING][info] unknown"
    assert "Alert 1/1:" in body


def test_render_email_handles_non_list_alerts(relay: ModuleType) -> None:
    payload = {
        "status": "resolved",
        "commonLabels": {"alertname": "X"},
        "alerts": "not-a-list",
    }
    subject, body = relay.render_email(payload)
    assert subject == "[RESOLVED][info] X"
    assert "Alert count: 0" in body


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def test_build_message_sets_headers_and_body(relay: ModuleType, cfg: Any) -> None:
    msg = relay.build_message(cfg, "subject!", "body text\n")
    assert isinstance(msg, EmailMessage)
    assert msg["From"] == cfg.smtp_user
    assert msg["To"] == cfg.smtp_to
    assert msg["Subject"] == "subject!"
    assert msg["Date"]
    assert msg["Message-ID"]
    assert msg["X-Mailer"] == "parallax-alertmanager-gmail-smtp-relay/1.0"
    assert msg.get_content().strip() == "body text"


def test_build_message_sanitizes_crlf_in_subject_when_called_directly(
    relay: ModuleType, cfg: Any
) -> None:
    """Defense in depth: build_message must not raise ValueError when a
    caller hands it a subject with embedded CR/LF (bypassing
    ``_format_subject``). Stdlib normally rejects such headers."""
    msg = relay.build_message(cfg, "first line\nsecond line\r\nthird\ttab", "ok\n")
    assert "\n" not in msg["Subject"]
    assert "\r" not in msg["Subject"]
    assert "\t" not in msg["Subject"]
    assert msg["Subject"] == "first line second line third tab"


# ---------------------------------------------------------------------------
# SMTP send (mock smtplib.SMTP) — instance state is per-test, owned by the
# ``smtp_recorder`` fixture so parallel test runners cannot collide.
# ---------------------------------------------------------------------------


class _SMTPRecorder:
    """Minimal stand-in for smtplib.SMTP used as a context manager."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        *,
        registry: list[_SMTPRecorder],
        raise_on: dict[str, BaseException],
        refused: dict[str, tuple[int, bytes]] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.sent: list[EmailMessage] = []
        self.raise_on = raise_on
        self.refused = refused or {}
        registry.append(self)

    def __enter__(self) -> _SMTPRecorder:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def _maybe_raise(self, name: str) -> None:
        exc = self.raise_on.get(name)
        if exc is not None:
            raise exc

    def ehlo(self) -> None:
        self.calls.append(("ehlo", ()))
        self._maybe_raise("ehlo")

    def starttls(self) -> None:
        self.calls.append(("starttls", ()))
        self._maybe_raise("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", (user, password)))
        self._maybe_raise("login")

    def send_message(self, msg: EmailMessage) -> dict[str, tuple[int, bytes]]:
        self.calls.append(("send_message", (msg,)))
        self._maybe_raise("send_message")
        self.sent.append(msg)
        return dict(self.refused)


class _SMTPHarness:
    """Per-test harness owning the SMTP factory state.

    Replaces the previous class-level ``_SMTPRecorder.instances`` list,
    which was shared across tests and would race under pytest-xdist.
    Supports ``len()``, ``[i]``, and ``== []`` so tests can keep their
    natural assertion style.
    """

    def __init__(self) -> None:
        self.instances: list[_SMTPRecorder] = []
        self.raise_on: dict[str, BaseException] = {}
        self.refused: dict[str, tuple[int, bytes]] = {}

    def __len__(self) -> int:
        return len(self.instances)

    def __getitem__(self, idx: int) -> _SMTPRecorder:
        return self.instances[idx]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return self.instances == other
        return NotImplemented

    def __hash__(self) -> int:  # pragma: no cover - paired with __eq__
        return id(self)


@pytest.fixture()
def smtp_recorder(relay: ModuleType, monkeypatch: pytest.MonkeyPatch) -> _SMTPHarness:
    """Install a fake ``smtplib.SMTP`` for the duration of the test."""
    harness = _SMTPHarness()

    def _factory(host: str, port: int, timeout: float) -> _SMTPRecorder:
        return _SMTPRecorder(
            host,
            port,
            timeout,
            registry=harness.instances,
            raise_on=harness.raise_on,
            refused=harness.refused,
        )

    monkeypatch.setattr(relay.smtplib, "SMTP", _factory)
    return harness


def _set_raise_on(harness: _SMTPHarness, raise_on: dict[str, BaseException]) -> None:
    harness.raise_on = raise_on


def _set_refused(harness: _SMTPHarness, refused: dict[str, tuple[int, bytes]]) -> None:
    harness.refused = refused


def test_send_email_calls_starttls_login_send_in_order(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    msg = relay.build_message(cfg, "hi", "ok\n")
    refused = relay.send_email(cfg, msg)
    assert refused == {}
    assert len(smtp_recorder) == 1
    rec = smtp_recorder[0]
    names = [c[0] for c in rec.calls]
    assert names == ["ehlo", "starttls", "ehlo", "login", "send_message"]
    assert rec.host == cfg.smtp_host
    assert rec.port == cfg.smtp_port
    assert rec.timeout == cfg.smtp_timeout_sec
    assert rec.sent[0] is msg


def test_send_email_propagates_smtp_auth_error(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    _set_raise_on(
        smtp_recorder,
        {"login": smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")},
    )
    msg = relay.build_message(cfg, "hi", "ok\n")
    with pytest.raises(smtplib.SMTPAuthenticationError):
        relay.send_email(cfg, msg)


def test_send_email_returns_refused_recipients_when_partial_delivery(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    _set_refused(smtp_recorder, {"bogus@example.com": (550, b"no such user")})
    msg = relay.build_message(cfg, "hi", "ok\n")
    refused = relay.send_email(cfg, msg)
    assert refused == {"bogus@example.com": (550, b"no such user")}


# ---------------------------------------------------------------------------
# Handler — exercise via a stub BaseHTTPRequestHandler harness
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Stand-in for the socket-side `request` argument BaseHTTPRequestHandler expects."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._read_buf = io.BytesIO(body)
        self._write_buf = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        if "r" in mode:
            return self._read_buf
        return self._write_buf

    # BaseHTTPRequestHandler wraps the request in a ``_SocketWriter`` (because
    # ``wbufsize == 0`` by default) and calls ``self.request.sendall(b)`` to
    # flush headers / body. Forward to our write buffer so the handler thinks
    # it's writing to a socket.
    def sendall(self, data: bytes) -> None:
        self._write_buf.write(data)

    def settimeout(self, _value: float | None) -> None:
        # _SocketWriter checks for this when wrapping the connection.
        return None

    def close(self) -> None:
        return None


def _build_post_request(payload: bytes, *, content_length: str | None = None) -> _FakeRequest:
    cl = str(len(payload)) if content_length is None else content_length
    body = b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: " + cl.encode()
    body += b"\r\nContent-Type: application/json\r\n\r\n"
    body += payload
    return _FakeRequest(body)


def _build_get_request(path: str) -> _FakeRequest:
    body = f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
    return _FakeRequest(body)


def _invoke_handler(handler_cls: type, request: _FakeRequest) -> tuple[int, dict[str, str], bytes]:
    """Run a single request through the handler and return (status, headers, body)."""
    handler_cls(request, ("127.0.0.1", 0), MagicMock())
    raw = request._write_buf.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("ascii", errors="replace")
    status_code = int(status_line.split(" ")[1])
    headers: dict[str, str] = {}
    for h in lines[1:]:
        key, _, value = h.decode("ascii", errors="replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    return status_code, headers, body


def test_handler_post_happy_path_returns_204_and_sends_email(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    handler_cls = relay.make_handler(cfg)
    payload = json.dumps(_firing_payload()).encode()
    request = _build_post_request(payload)

    status, _, _ = _invoke_handler(handler_cls, request)
    assert status == 204
    assert len(smtp_recorder) == 1
    rec = smtp_recorder[0]
    assert len(rec.sent) == 1
    sent = rec.sent[0]
    assert sent["To"] == cfg.smtp_to
    assert sent["From"] == cfg.smtp_user
    assert "[FIRING][critical] AphelionQ8Drain" in sent["Subject"]


@pytest.mark.parametrize(
    "exc",
    [
        smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials"),
        smtplib.SMTPException("rejected"),
        TimeoutError("smtp timeout"),
        OSError("dns fail"),
    ],
)
def test_handler_post_smtp_failure_returns_500_for_alertmanager_retry(
    relay: ModuleType,
    cfg: Any,
    smtp_recorder: _SMTPHarness,
    exc: BaseException,
) -> None:
    _set_raise_on(smtp_recorder, {"send_message": exc})
    handler_cls = relay.make_handler(cfg)
    payload = json.dumps(_firing_payload()).encode()
    request = _build_post_request(payload)

    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 500, f"expected 500 retry signal for {type(exc).__name__}"
    assert b"smtp send failed:" in body
    assert type(exc).__name__.encode() in body


def test_handler_post_partial_delivery_returns_500(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    _set_refused(smtp_recorder, {"oncall@example.com": (550, b"user unknown")})
    handler_cls = relay.make_handler(cfg)
    payload = json.dumps(_firing_payload()).encode()
    request = _build_post_request(payload)

    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 500
    assert b"smtp partial delivery" in body


def test_handler_post_bad_json_returns_400(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    handler_cls = relay.make_handler(cfg)
    request = _build_post_request(b"{not-json")
    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 400
    assert b"bad json" in body
    # No SMTP call attempted
    assert smtp_recorder == []


def test_handler_post_non_object_returns_400(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    handler_cls = relay.make_handler(cfg)
    request = _build_post_request(b"[1,2,3]")
    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 400
    assert b"bad payload" in body
    assert smtp_recorder == []


def test_handler_post_empty_body_returns_400_and_does_not_send(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    """Content-Length 0 -> 400 (refusing to email a blank alert)."""
    handler_cls = relay.make_handler(cfg)
    request = _build_post_request(b"")
    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 400
    assert b"empty payload" in body
    assert smtp_recorder == []


def test_handler_post_oversized_body_returns_413(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    """Content-Length above MAX_BODY_BYTES -> 413 without reading the body."""
    handler_cls = relay.make_handler(cfg)
    over = relay.MAX_BODY_BYTES + 1
    # Body bytes are short — we only need the declared Content-Length to trip the gate.
    request = _build_post_request(b"x", content_length=str(over))
    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 413
    assert b"payload too large" in body
    assert smtp_recorder == []


def test_handler_post_garbage_content_length_returns_400(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    handler_cls = relay.make_handler(cfg)
    request = _build_post_request(b"{}", content_length="abc")
    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 400
    assert b"bad Content-Length" in body
    assert smtp_recorder == []


def test_handler_post_negative_content_length_returns_400(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    handler_cls = relay.make_handler(cfg)
    request = _build_post_request(b"{}", content_length="-5")
    status, _, body = _invoke_handler(handler_cls, request)
    assert status == 400
    assert b"bad Content-Length" in body
    assert smtp_recorder == []


def test_handler_healthz_returns_200(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    handler_cls = relay.make_handler(cfg)
    status, _, body = _invoke_handler(handler_cls, _build_get_request("/healthz"))
    assert status == 200
    assert body.strip() == b"ok"


def test_handler_unknown_path_returns_404(
    relay: ModuleType, cfg: Any, smtp_recorder: _SMTPHarness
) -> None:
    handler_cls = relay.make_handler(cfg)
    status, _, _ = _invoke_handler(handler_cls, _build_get_request("/nope"))
    assert status == 404


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

_REQUIRED_ENVS = ("GMAIL_SMTP_USER", "GMAIL_SMTP_APP_PASSWORD", "GMAIL_SMTP_TO")


def _set_required_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_SMTP_USER", "alerts@example.com")
    monkeypatch.setenv("GMAIL_SMTP_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("GMAIL_SMTP_TO", "oncall@example.com")


def test_load_config_returns_dataclass_with_defaults(
    relay: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_required_envs(monkeypatch)
    for name in (
        "GMAIL_SMTP_HOST",
        "GMAIL_SMTP_PORT",
        "GMAIL_SMTP_TIMEOUT",
        "RELAY_HOST",
        "RELAY_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = relay.load_config()
    assert cfg.smtp_user == "alerts@example.com"
    assert cfg.smtp_app_password == "abcdefghijklmnop"
    assert cfg.smtp_to == "oncall@example.com"
    assert cfg.smtp_host == "smtp.gmail.com"
    assert cfg.smtp_port == 587
    assert cfg.smtp_timeout_sec == 10.0
    assert cfg.listen_host == "127.0.0.1"
    assert cfg.listen_port == 9096


def test_load_config_honors_overrides(relay: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_envs(monkeypatch)
    monkeypatch.setenv("GMAIL_SMTP_HOST", "smtp.internal")
    monkeypatch.setenv("GMAIL_SMTP_PORT", "2525")
    monkeypatch.setenv("GMAIL_SMTP_TIMEOUT", "3.5")
    monkeypatch.setenv("RELAY_HOST", "0.0.0.0")
    monkeypatch.setenv("RELAY_PORT", "19096")
    cfg = relay.load_config()
    assert cfg.smtp_host == "smtp.internal"
    assert cfg.smtp_port == 2525
    assert cfg.smtp_timeout_sec == 3.5
    assert cfg.listen_host == "0.0.0.0"
    assert cfg.listen_port == 19096


@pytest.mark.parametrize("missing", _REQUIRED_ENVS)
def test_load_config_missing_required_env_raises_system_exit(
    relay: ModuleType, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _set_required_envs(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(SystemExit) as ei:
        relay.load_config()
    assert missing in str(ei.value)


@pytest.mark.parametrize("var", ("GMAIL_SMTP_USER", "GMAIL_SMTP_TO"))
@pytest.mark.parametrize(
    "poison", ("user@example.com\r\nBcc: attacker@example.com", "to@example.com\n")
)
def test_load_config_rejects_crlf_in_header_envs(
    relay: ModuleType, monkeypatch: pytest.MonkeyPatch, var: str, poison: str
) -> None:
    _set_required_envs(monkeypatch)
    monkeypatch.setenv(var, poison)
    with pytest.raises(SystemExit) as ei:
        relay.load_config()
    assert "CR/LF" in str(ei.value)


# ---------------------------------------------------------------------------
# main wiring — exercise the server bind path without serving forever
# ---------------------------------------------------------------------------


def test_main_binds_threading_server_and_returns_on_keyboard_interrupt(
    relay: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_required_envs(monkeypatch)

    constructed: dict[str, Any] = {}

    class _FakeServer:
        def __init__(self, address: tuple[str, int], handler_cls: type) -> None:
            constructed["address"] = address
            constructed["handler_cls"] = handler_cls

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(relay, "ThreadingHTTPServer", _FakeServer)
    # Should return cleanly (KeyboardInterrupt is suppressed).
    relay.main()
    assert constructed["address"] == ("127.0.0.1", 9096)
    assert callable(constructed["handler_cls"])


# ---------------------------------------------------------------------------
# Static file assertions — ensures the env example never contains real secrets
# and the systemd unit + relay script stay paired.
# ---------------------------------------------------------------------------


def test_env_example_has_no_secret_values() -> None:
    env_example = (REPO_ROOT / "scripts" / "gmail-smtp-relay.env.example").read_text(
        encoding="utf-8"
    )
    for key in _REQUIRED_ENVS:
        # Each required key must be present and empty (KEY= with no value on the line).
        assert any(
            line.strip() == f"{key}=" for line in env_example.splitlines()
        ), f"{key} must be present and empty in env example"


def test_systemd_unit_points_at_relay_script() -> None:
    unit = (REPO_ROOT / "deploy" / "systemd" / "parallax-gmail-smtp-relay.service").read_text(
        encoding="utf-8"
    )
    assert "gmail-smtp-relay.py" in unit
    assert "gmail-smtp-relay.env" in unit
    assert "EnvironmentFile=" in unit
    # %h keeps the unit portable; a hardcoded /home/<name> would be a regression.
    assert "%h/" in unit
    assert "/home/chris/" not in unit


def test_systemd_unit_places_start_limits_in_unit_section() -> None:
    """``StartLimitBurst`` / ``StartLimitIntervalSec`` belong in [Unit].

    systemd >= 230 reads these from the [Unit] section. Placing them in
    [Service] silently no-ops on systemd >= 255 (verified by
    `systemd-analyze verify`), which would defeat the crash-loop cap that
    is the entire reason we set them.
    """
    unit = (REPO_ROOT / "deploy" / "systemd" / "parallax-gmail-smtp-relay.service").read_text(
        encoding="utf-8"
    )

    # Split into ini-style sections by lines that start with "[".
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in unit.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    unit_lines = sections.get("Unit", [])
    service_lines = sections.get("Service", [])

    def _has_directive(lines: list[str], key: str) -> bool:
        return any(ln.strip().startswith(f"{key}=") for ln in lines)

    assert _has_directive(unit_lines, "StartLimitBurst"), "StartLimitBurst must be in [Unit]"
    assert _has_directive(
        unit_lines, "StartLimitIntervalSec"
    ), "StartLimitIntervalSec must be in [Unit]"
    # And NOT duplicated into [Service] (where systemd >=255 silently ignores them).
    assert not _has_directive(service_lines, "StartLimitBurst")
    assert not _has_directive(service_lines, "StartLimitIntervalSec")


def test_alertmanager_config_registers_gmail_smtp_receiver() -> None:
    cfg_text = (REPO_ROOT / "deploy" / "observability" / "alertmanager.yml").read_text(
        encoding="utf-8"
    )
    assert "name: gmail-smtp" in cfg_text
    assert "http://127.0.0.1:9096/" in cfg_text
