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

Usage::

    PARALLAX_BURN_IN_ENDPOINT="http://127.0.0.1:8000/query" \\
    PARALLAX_BURN_IN_USER_ID="parallax-burn-in-synth" \\
    PARALLAX_BURN_IN_FIXTURE=/path/to/m3_corpus.json \\
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
HEADERS = {
    "X-Parallax-Traffic-Source": "synthetic",
    "X-Parallax-Synth-Marker": "burn-in-loader-v1",
}

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


def run_loader(
    *,
    endpoint: str,
    sample_keys: Sequence[str],
    user_id: str,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    error_budget: int = CONSECUTIVE_ERROR_BUDGET,
    client_error_budget: int = CONSECUTIVE_CLIENT_ERROR_BUDGET,
    iterations: int | None = None,
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
    client = httpx.Client(timeout=5.0, headers=HEADERS)
    consecutive_errors = 0          # 5xx + httpx exceptions
    consecutive_client_errors = 0   # 4xx
    idx = 0
    sent = 0
    try:
        while True:
            key = sample_keys[idx % len(sample_keys)]
            idx += 1
            try:
                response = client.get(f"{endpoint}?kind=recent&q={key}&user_id={user_id}")
                status = response.status_code
                if status >= 500:
                    consecutive_errors += 1
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
                        return 75
                elif response.is_error:
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
                        return 75
                else:
                    LOG.info("synth_qry key=%s status=%d", key, status)
                    consecutive_errors = 0
                    consecutive_client_errors = 0
            except httpx.HTTPError as exc:
                consecutive_errors += 1
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
                    return 75
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
    LOG.info(
        "synth loader starting endpoint=%s user_id=%s fixture=%s keys=%d",
        endpoint,
        user_id,
        fixture_path,
        len(sample_keys),
    )
    return run_loader(
        endpoint=endpoint,
        sample_keys=sample_keys,
        user_id=user_id,
        interval_seconds=_interval_seconds(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
