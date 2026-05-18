#!/usr/bin/env python3
"""Alertmanager → Discord embed relay.

Listens on RELAY_HOST:RELAY_PORT (default 127.0.0.1:9095), accepts
Alertmanager webhook payload, converts to Discord embed format, forwards
to the URL in DISCORD_WEBHOOK env var.

Why not use Alertmanager's `slack_configs` with Discord's `/slack` shim?
Alertmanager 0.27 auto-fills attachment fields (image_url="", thumb_url="",
mrkdwn_in=[...]) that Discord's embed validator rejects with HTTP 400.
There is no Alertmanager knob to omit those fields, so a tiny stdlib
relay is the path of least friction.

Why not Python urllib defaults? Discord's Cloudflare WAF returns HTTP 403
(error 1010) for the default `Python-urllib/3.x` User-Agent. We override
it below — any non-default UA works.

Stdlib only — no pip deps.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")
if not DISCORD_WEBHOOK:
    print("FATAL: DISCORD_WEBHOOK env not set", file=sys.stderr)
    sys.exit(1)

LISTEN_HOST: str = os.environ.get("RELAY_HOST", "127.0.0.1")
LISTEN_PORT: int = int(os.environ.get("RELAY_PORT", "9095"))

# Discord embed color (0xRRGGBB int, NOT "#hex" string — Discord rejects strings).
SEVERITY_COLOR: dict[str, int] = {
    "critical": 0xA30200,
    "warning": 0xDAA038,
    "info": 0x2EB886,
    "page": 0xA30200,
}
STATUS_COLOR: dict[str, int] = {"resolved": 0x2EB886, "firing": 0xA30200}

# Discord embed shape limits — see https://discord.com/developers/docs/resources/channel#embed-limits
EMBED_TITLE_MAX = 256
EMBED_DESC_MAX = 4096
EMBED_FIELD_VALUE_MAX = 1024
EMBED_FIELDS_MAX = 25
EMBEDS_PER_MESSAGE_MAX = 10

# Label keys we surface as embed fields, in display order.
SURFACED_LABELS = ("severity", "rollback_class", "trigger", "instance", "job")


def to_discord(am_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert an Alertmanager webhook payload to a Discord webhook payload."""
    embeds: list[dict[str, Any]] = []
    status = am_payload.get("status", "firing")
    for alert in am_payload.get("alerts", []):
        labels = alert.get("labels", {})
        ann = alert.get("annotations", {})
        sev = labels.get("severity", "info")
        if status == "resolved":
            color = STATUS_COLOR["resolved"]
        else:
            color = SEVERITY_COLOR.get(sev, 0x808080)
        title = f"[{status.upper()}] {labels.get('alertname', 'unknown')}"[:EMBED_TITLE_MAX]
        description = (ann.get("summary") or ann.get("description") or "")[:EMBED_DESC_MAX]
        fields: list[dict[str, Any]] = []
        for k in SURFACED_LABELS:
            if k in labels:
                fields.append(
                    {
                        "name": k,
                        "value": str(labels[k])[:EMBED_FIELD_VALUE_MAX],
                        "inline": True,
                    }
                )
        fields = fields[:EMBED_FIELDS_MAX]
        embed: dict[str, Any] = {
            "title": title,
            "description": description,
            "color": color,
        }
        if fields:
            embed["fields"] = fields
        embeds.append(embed)
        if len(embeds) >= EMBEDS_PER_MESSAGE_MAX:
            break
    return {"embeds": embeds} if embeds else {"content": "(empty alert group)"}


class Handler(BaseHTTPRequestHandler):
    """Receives Alertmanager webhooks, fans them out to Discord as embeds."""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(f"bad json: {exc}".encode())
            return
        payload = to_discord(body)
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                # Discord/Cloudflare blocks the default Python-urllib UA.
                "User-Agent": "parallax-alertmanager-relay/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — fixed scheme
                resp.read()
            self.send_response(204)
            self.end_headers()
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:512]
            print(f"discord HTTPError {exc.code}: {err_body}", file=sys.stderr)
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"discord {exc.code}: {err_body}".encode())
        except Exception as exc:  # noqa: BLE001 — relay must not crash on Discord transient
            print(f"relay error: {exc}", file=sys.stderr)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(exc).encode())

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 — base override
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    srv = HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"discord-relay listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
