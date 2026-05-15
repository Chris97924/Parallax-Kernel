"""Apex M4 burn-in synthetic loader — 1 qps localhost dual-read driver.

Purpose: keep B1/B2 Prometheus metric series alive during the M4 burn-in
window when natural production traffic is zero. Pairs with the hybrid
DoD logic in ``parallax canary --dod`` (see
``docs/m4-prep/traffic-gap-resolution.md`` §3.3).

Every metric emitted server-side as a result of this loader's queries
MUST carry ``traffic_source="synthetic"``. The server-side
``TrafficSourceMiddleware`` reads the ``X-Parallax-Traffic-Source``
header and labels accordingly (see
``parallax/server/middleware/traffic_source.py``).

Authentication: when ``PARALLAX_BURN_IN_TOKEN`` is set, the loader sends
``Authorization: Bearer <token>`` so it can drive a server that has
``PARALLAX_TOKEN`` configured. The pre-flight health check verifies the
token works before entering the 1 qps loop — a 401 at startup exits with
``EX_NOPERM`` (77) rather than entering the 4xx-retry budget. See
xcouncil 2026-05-15 Q2 verdict B and traffic-gap-resolution.md §5.

Usage::

    PARALLAX_BURN_IN_ENDPOINT="http://127.0.0.1:8000/query" \\
    PARALLAX_BURN_IN_USER_ID="parallax-burn-in-synth" \\
    PARALLAX_BURN_IN_FIXTURE=/path/to/m3_corpus.json \\
    PARALLAX_BURN_IN_TOKEN="..."   # optional — sent as Bearer
    python scripts/burn-in-synth-loader.py

The fixture is a JSON list of M3 corpus claim ids. The script aborts
with a non-zero exit if the fixture is missing or empty — synthetic
load against an unrelated key set produces meaningless metrics.

systemd unit: ``deploy/systemd/parallax-burn-in-synth-loader.service``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from collections.abc import Sequence
from pathlib import Path

import httpx

LOG = logging.getLogger("parallax.burn_in.synth_loader")

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/query"
BASE_HEADERS = {
    "X-Parallax-Traffic-Source": "synthetic",
    "X-Parallax-Synth-Marker": "burn-in-loader-v1",
}

# sysexits — 77 = EX_NOPERM (permission denied; auth misconfig)
EXIT_NOPERM = 77
# sysexits — 75 = EX_TEMPFAIL (transient — systemd Restart=always re-launches)
EXIT_TEMPFAIL = 75

# Spec §5: after this many consecutive errors, exit so systemd
# Restart=always fires rather than silently looping at 1 qps with the
# metric series going dark.
CONSECUTIVE_ERROR_BUDGET = 30

# 4xx budget is much higher than the 5xx/transport budget — most 4xx
# responses are configuration drift (auth not yet provisioned, query-shape
# mismatch) that retrying won't fix, so we don't want to flap the systemd
# unit on transient blips. But a *persistent* 4xx (e.g. 401 from a
# permanently-misconfigured auth setup) must eventually exit so systemd's
# Restart=always cycles the process and the failure surfaces in journals
# instead of silently stalling burn-in.
CONSECUTIVE_CLIENT_ERROR_BUDGET = 300

DEFAULT_INTERVAL_SECONDS = 1.0


def _resolve_endpoint() -> str:
    return os.environ.get("PARALLAX_BURN_IN_ENDPOINT", DEFAULT_ENDPOINT)


def _resolve_user_id() -> str:
    raw = os.environ.get("PARALLAX_BURN_IN_USER_ID", "parallax-burn-in-synth")
    if not raw:
        raise SystemExit(
            "PARALLAX_BURN_IN_USER_ID must be a non-empty string; "
            "synthetic loader has no auth so user_id is required by /query."
        )
    return raw


def _resolve_fixture_path() -> Path:
    raw = os.environ.get("PARALLAX_BURN_IN_FIXTURE")
    if not raw:
        raise SystemExit(
            "PARALLAX_BURN_IN_FIXTURE env required — point at a JSON list "
            "of M3 corpus claim ids; see traffic-gap-resolution.md §3.1."
        )
    path = Path(raw)
    if not path.exists():
        raise SystemExit(f"fixture not found: {path}")
    return path


def _load_fixture(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise SystemExit(
            f"fixture {path} must be a JSON list of strings (claim ids)"
        )
    if not raw:
        raise SystemExit(
            f"fixture {path} is empty — synthetic load against zero keys "
            "produces vacuous metrics; populate the M3 corpus first."
        )
    return raw


def _interval_seconds() -> float:
    raw = os.environ.get("PARALLAX_BURN_IN_INTERVAL_SECONDS")
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid PARALLAX_BURN_IN_INTERVAL_SECONDS: {raw!r}") from exc
    if value <= 0:
        raise SystemExit("PARALLAX_BURN_IN_INTERVAL_SECONDS must be > 0")
    return value


def _resolve_bearer_token() -> str | None:
    """Return the bearer token to send, or ``None`` for no Authorization header.

    Precedence: ``PARALLAX_BURN_IN_TOKEN`` > ``PARALLAX_TOKEN``. The dedicated
    burn-in env var lets ops grant the synth loader a separate credential
    from production traffic (smaller blast radius, easier rotation per
    xcouncil 2026-05-15 Q2 verdict B).

    Whitespace-only values are treated as unset and DO fall through to the
    next variable in the precedence chain (Codex 2026-05-15 round-2 P1
    finding): a previous one-liner used ``A or B`` which short-circuits on
    truthy-but-whitespace strings before ``B`` is ever consulted. We now
    strip each candidate independently and only fall through to the next
    when the current one is empty after stripping.
    """
    for env_name in ("PARALLAX_BURN_IN_TOKEN", "PARALLAX_TOKEN"):
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        stripped = raw.strip()
        if stripped:
            return stripped
    return None


def _build_headers(token: str | None) -> dict[str, str]:
    """Return loader request headers, optionally including the bearer token."""
    headers = dict(BASE_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def preflight_check(
    *,
    endpoint: str,
    headers: dict[str, str],
    user_id: str,
    sample_key: str,
    timeout_seconds: float = 5.0,
) -> int:
    """Verify the loader credentials work before entering the 1 qps loop.

    Sends a single throw-away ``/query`` GET. A ``401`` is a permanent
    authentication misconfiguration that the 4xx retry budget cannot
    fix — return ``EXIT_NOPERM`` (77) so systemd ``Restart=always`` surfaces
    the failure in journals rather than spinning at 1 qps with the metric
    series going dark for ~5 minutes.

    Non-auth failures (network, 5xx, 4xx other than 401) fall through to
    the normal loop with its budget logic; only 401 is fatal here.

    Returns:
        ``0`` if the loader may proceed (auth OK or fast-fail not warranted).
        ``EXIT_NOPERM`` (77) if a 401 was observed — caller MUST exit.
    """
    try:
        with httpx.Client(timeout=timeout_seconds, headers=headers) as client:
            response = client.get(
                endpoint,
                params={"kind": "recent", "q": sample_key, "user_id": user_id},
            )
    except httpx.HTTPError as exc:
        LOG.warning(
            "preflight network failure (will rely on loop budget) "
            "endpoint=%s exc=%s msg=%s",
            endpoint, exc.__class__.__name__, exc,
        )
        return 0
    if response.status_code == 401:
        LOG.critical(
            "preflight auth check returned 401 — bearer token missing or "
            "invalid (endpoint=%s). Exiting with EX_NOPERM=77; ensure "
            "PARALLAX_BURN_IN_TOKEN is set before restarting.",
            endpoint,
        )
        return EXIT_NOPERM
    LOG.info(
        "preflight OK endpoint=%s status=%d auth=%s",
        endpoint,
        response.status_code,
        "bearer" if "Authorization" in headers else "none",
    )
    return 0


def run_loader(
    *,
    endpoint: str,
    sample_keys: Sequence[str],
    user_id: str,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    error_budget: int = CONSECUTIVE_ERROR_BUDGET,
    client_error_budget: int = CONSECUTIVE_CLIENT_ERROR_BUDGET,
    iterations: int | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    """Drive ``endpoint`` at ~1 qps with the synthetic header.

    Args:
        endpoint: full HTTP URL for the dual-read endpoint.
        sample_keys: M3 corpus claim ids — round-robined across queries.
        interval_seconds: delay between requests.
        error_budget: consecutive 5xx/transport-error threshold; on
            exhaustion the function returns ``75`` (EX_TEMPFAIL) so
            systemd ``Restart=always`` re-launches a fresh process.
        client_error_budget: consecutive 4xx-error threshold; much higher
            than ``error_budget`` because 4xx usually indicates config
            drift that retrying won't fix immediately (auth not yet
            provisioned, query-shape mismatch).  A *persistent* 4xx
            (e.g. 401 from permanently-misconfigured auth) still
            eventually exits so systemd cycles the unit and the failure
            surfaces in journals instead of silently stalling burn-in.
        iterations: if provided, exit after this many requests (test hook).

    Returns:
        process exit code: ``0`` on completion (only possible when
        ``iterations`` is set), ``75`` on error-budget exhaustion.
    """
    if not sample_keys:
        return 2
    client = httpx.Client(timeout=5.0, headers=headers if headers is not None else BASE_HEADERS)
    consecutive_errors = 0          # 5xx + httpx exceptions
    consecutive_client_errors = 0   # 4xx
    idx = 0
    sent = 0
    try:
        while True:
            key = sample_keys[idx % len(sample_keys)]
            idx += 1
            try:
                response = client.get(
                    endpoint,
                    params={"kind": "recent", "q": key, "user_id": user_id},
                )
                status = response.status_code
                if status >= 500:
                    consecutive_errors += 1
                    consecutive_client_errors = 0  # 5xx breaks any 4xx streak
                    LOG.warning(
                        "synth_qry key=%s status=%d consecutive_5xx=%d",
                        key, status, consecutive_errors,
                    )
                    if consecutive_errors >= error_budget:
                        LOG.critical(
                            "synth loader exhausted 5xx budget=%d; exiting "
                            "so systemd Restart=always re-launches",
                            error_budget,
                        )
                        return EXIT_TEMPFAIL
                elif response.is_error:
                    consecutive_errors = 0
                    consecutive_client_errors += 1
                    LOG.warning(
                        "synth_qry key=%s status=%d consecutive_4xx=%d",
                        key, status, consecutive_client_errors,
                    )
                    if consecutive_client_errors >= client_error_budget:
                        LOG.critical(
                            "synth loader exhausted 4xx budget=%d "
                            "(persistent client error, e.g. auth misconfig); "
                            "exiting so systemd Restart=always re-launches",
                            client_error_budget,
                        )
                        return EXIT_TEMPFAIL
                elif status < 300:
                    LOG.info("synth_qry key=%s status=%d", key, status)
                    consecutive_errors = 0
                    consecutive_client_errors = 0
                else:
                    # 3xx redirect — resets 5xx streak but increments
                    # client-error counter (treated as misconfiguration).
                    consecutive_errors = 0
                    consecutive_client_errors += 1
                    LOG.warning(
                        "synth_qry key=%s status=%d (redirect; endpoint "
                        "misconfigured?) consecutive_4xx=%d",
                        key, status, consecutive_client_errors,
                    )
                    if consecutive_client_errors >= client_error_budget:
                        LOG.critical(
                            "synth loader exhausted 4xx budget=%d "
                            "(persistent redirect, e.g. trailing-slash "
                            "misconfig); exiting so systemd Restart=always "
                            "re-launches",
                            client_error_budget,
                        )
                        return EXIT_TEMPFAIL
            except httpx.HTTPError as exc:
                consecutive_errors += 1
                consecutive_client_errors = 0  # transport error breaks any 4xx streak
                LOG.error(
                    "synth_qry key=%s exc=%s msg=%s consecutive=%d",
                    key,
                    exc.__class__.__name__,
                    exc,
                    consecutive_errors,
                )
                if consecutive_errors >= error_budget:
                    LOG.critical(
                        "synth loader exhausted error budget=%d; exiting "
                        "so systemd Restart=always re-launches",
                        error_budget,
                    )
                    return EXIT_TEMPFAIL
            sent += 1
            if iterations is not None and sent >= iterations:
                return 0
            time.sleep(interval_seconds)
    finally:
        client.close()


def _handle_sigterm(signum: int, frame: object) -> None:  # noqa: ARG001
    raise KeyboardInterrupt()


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
    )
    endpoint = _resolve_endpoint()
    user_id = _resolve_user_id()
    fixture_path = _resolve_fixture_path()
    sample_keys = _load_fixture(fixture_path)
    token = _resolve_bearer_token()
    headers = _build_headers(token)
    LOG.info(
        "synth loader starting endpoint=%s user_id=%s fixture=%s keys=%d auth=%s",
        endpoint,
        user_id,
        fixture_path,
        len(sample_keys),
        "bearer" if token else "none",
    )
    # Pre-flight: catch auth misconfig before the 4xx-budget eats 5 minutes
    # of metric-series darkness. A 401 here is fatal; other failures fall
    # through to the loop with its budget logic.
    preflight_exit = preflight_check(
        endpoint=endpoint,
        headers=headers,
        user_id=user_id,
        sample_key=sample_keys[0],
    )
    if preflight_exit != 0:
        return preflight_exit
    return run_loader(
        endpoint=endpoint,
        sample_keys=sample_keys,
        user_id=user_id,
        interval_seconds=_interval_seconds(),
        headers=headers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
