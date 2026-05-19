# Alertmanager → Gmail SMTP relay

Companion to the Discord relay. Same host-local-webhook shape, different
egress channel. Where the Discord relay forwards alertmanager webhooks
to a channel webhook URL, this one composes a plain-text email and sends
it through Gmail SMTP using a 2FA App Password.

## Why a relay, not Alertmanager `email_configs`?

Alertmanager's built-in `email_configs` reads the SMTP password from
`alertmanager.yml`. That file is bind-mounted read-only into the
alertmanager container, so storing the App Password there means the
password lives in the repo (or in a sibling secret file the deploy
script has to interleave at bind time — fragile). Routing through a
host-local relay keeps the credential in `/etc/parallax/gmail-smtp-relay.env`
(chmod 600, root-owned), gives us a single journald surface for both
notification paths, and lets us reuse the relay testing pattern.

## Architecture

```
Alertmanager (container, port 9093)
        │  webhook_configs[].url = http://127.0.0.1:9096/
        ▼
gmail-smtp-relay.py (systemd-user, port 9096)
        │  smtplib.SMTP(smtp.gmail.com:587).starttls().login().send_message()
        ▼
Gmail account inbox (GMAIL_SMTP_TO)
```

Both relays listen on `127.0.0.1` only — there is no external surface.
Alertmanager runs in `network_mode: host`, so it reaches the relay
through the loopback interface.

## File layout

| Purpose | Path |
| --- | --- |
| Relay script | `scripts/gmail-smtp-relay.py` |
| Env example | `scripts/gmail-smtp-relay.env.example` |
| systemd unit | `deploy/systemd/parallax-gmail-smtp-relay.service` |
| Alertmanager receiver entry | `deploy/observability/alertmanager.yml` (`name: gmail-smtp`) |
| Tests | `tests/scripts/test_gmail_smtp_relay.py` |

## One-time setup (ZenBook)

> **NOT auto-deployed.** This PR ships code only; M5 burn-in clock is
> intentionally untouched. Run the steps below manually when Chris is
> ready to flip the email channel on.

### 1. Generate a Gmail App Password

1. Account must have 2-step verification enabled
   (https://myaccount.google.com/security).
2. Go to https://myaccount.google.com/apppasswords.
3. App = "Mail", Device = "ZenBook alertmanager". Copy the 16-character
   password Google displays.
4. The password is shown once. If lost, revoke it and issue a new one.

### 2. Place the env file

The systemd unit reads `EnvironmentFile=%h/parallax-kernel/scripts/gmail-smtp-relay.env`,
where `%h` is the user's home (i.e. `/home/chris/...` for Chris's account
under `systemctl --user`). Keep the file colocated with the script:

```bash
install -m 600 scripts/gmail-smtp-relay.env.example \
                ~/parallax-kernel/scripts/gmail-smtp-relay.env
$EDITOR ~/parallax-kernel/scripts/gmail-smtp-relay.env
```

Fill in `GMAIL_SMTP_USER`, `GMAIL_SMTP_APP_PASSWORD`, `GMAIL_SMTP_TO`.

> The repo `.gitignore` already covers `*.env` and explicitly excludes
> `!*.env.example`, so the example file is the only one that should
> ever appear in `git status`. Verify with `git status`.

> **Do not** set `RELAY_HOST=0.0.0.0`. The relay has no webhook auth — it
> assumes loopback-only traffic. Binding to a non-loopback interface
> exposes the SMTP relay endpoint (and thus an indirect way to spend
> your Gmail send quota) to anything that can reach the host.

### 3. Install the systemd-user unit

> The Discord relay runs as a systemd-user unit under `chris`. Mirror
> that. If you decide to migrate both to system units later, do them in
> the same change so the topology stays uniform.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/parallax-gmail-smtp-relay.service \
   ~/.config/systemd/user/parallax-gmail-smtp-relay.service
systemctl --user daemon-reload
systemctl --user enable --now parallax-gmail-smtp-relay.service
systemctl --user status parallax-gmail-smtp-relay.service
```

### 4. Reload the alertmanager config

Do **not** `sed -i` the bind-mounted config. Edit the file on the host,
then signal the container:

```bash
docker exec parallax-alertmanager kill -HUP 1
# or
curl -X POST http://127.0.0.1:9093/-/reload
```

The new `gmail-smtp` receiver appears in
`http://127.0.0.1:9093/#/status` under "Config".

### 5. Wire a route

This PR ships the receiver but does **not** wire it into the `route`
tree, on purpose — the first real send should be a controlled smoke
test, not a surprise during an actual incident. When ready, add a
matchers block like:

```yaml
route:
  routes:
    - matchers:
        - severity = "critical"
      receiver: deadletter-critical
      continue: true
    - matchers:
        - severity = "critical"
      receiver: gmail-smtp
      continue: false
```

`continue: true` on the first match makes the alert fall through to
gmail-smtp as well.

## Smoke test

With the relay running, drop a synthetic alert directly into the relay
(bypass alertmanager) so you can verify SMTP without involving the rule
chain:

```bash
curl -sS -XPOST http://127.0.0.1:9096/ \
  -H 'Content-Type: application/json' \
  -d '{
    "version": "4",
    "status": "firing",
    "groupKey": "{}:{alertname=\"SmokeTest\"}",
    "receiver": "gmail-smtp",
    "commonLabels": {"alertname": "SmokeTest", "severity": "info"},
    "commonAnnotations": {"summary": "manual smoke test"},
    "alerts": [{
      "status": "firing",
      "labels": {"alertname": "SmokeTest", "severity": "info"},
      "annotations": {"summary": "manual smoke test"},
      "startsAt": "2026-05-19T00:00:00Z"
    }]
  }'
```

Expect:
- HTTP **204** from `curl -i` (the relay returns 204 No Content on success).
- A new email in `GMAIL_SMTP_TO` within ~5s, subject
  `[FIRING][info] SmokeTest - manual smoke test`.

If you instead get HTTP 500, check `journalctl --user -u parallax-gmail-smtp-relay -e`
for the SMTP-side error (auth fail, timeout, etc.).

## Failure modes

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Relay logs `FATAL: GMAIL_SMTP_* env not set` and unit enters `failed` | Env file missing or wrong path | Check `EnvironmentFile=` matches the actual env file path |
| Relay returns 500 with `SMTPAuthenticationError` | App Password revoked or wrong account | Re-issue at https://myaccount.google.com/apppasswords and update the env file |
| Relay returns 500 with `TimeoutError` | Network egress to `smtp.gmail.com:587` blocked | Verify firewall / NAT / corporate proxy |
| Alertmanager reports `webhook integration unavailable` | Relay not running | `systemctl --user status parallax-gmail-smtp-relay.service` |
| Alertmanager retries forever on a single alert | Relay returns 500 on every attempt | Look at the alert payload in alertmanager UI; check journald for the relay's stderr |

The relay deliberately returns **HTTP 500 on any SMTP-level failure**
(auth, TLS, timeout, send rejection). Alertmanager interprets that as
"please retry later" and will keep the alert in its send queue per the
configured `group_interval` / `repeat_interval`. That's the right
behavior — we'd rather get a delayed page than silently drop a
critical alert because Gmail had a transient hiccup.

## Security notes

- The App Password is the only credential. Treat it as a full-mailbox
  compromise if leaked.
- The relay listens on `127.0.0.1` only. There's no auth on the webhook
  endpoint because the threat model assumes nothing local-only-trusted
  is hostile; if the host is compromised, the App Password is already
  readable from `/etc/parallax/gmail-smtp-relay.env` regardless.
- Repo grep verification: `git grep -nE 'GMAIL_SMTP_APP_PASSWORD=[^[:space:]]+'`
  must return nothing committed.

## Related

- [Discord relay](../m3-runbooks/observability-cookbook.md) — same
  topology, different egress.
- `scripts/discord-relay.py` — the relay this one is patterned on.
- `deploy/observability/alertmanager.yml` — the receiver registry.
