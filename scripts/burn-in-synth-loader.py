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

    PARALLAX_BURN_IN_ENDPOINT="http://127.0.0.1:8000/v1/dual-read" \\
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
import time
from collections.abc import Sequence
from pathlib import Path

import httpx

LOG = logging.getLogger("parallax.burn_in.synth_loader")

DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1/dual-read"
HEADERS = {
    "X-Parallax-Traffic-Source": "synthetic",
    "X-Parallax-Synth-Marker": "burn-in-loader-v1",
}

# Spec §5: after this many consecutive errors, exit so systemd
# Restart=always fires rather than silently looping at 1 qps with the
# metric series going dark.
CONSECUTIVE_ERROR_BUDGET = 30
DEFAULT_INTERVAL_SECONDS = 1.0


def _resolve_endpoint() -> str:
    return os.environ.get("PARALLAX_BURN_IN_ENDPOINT", DEFAULT_ENDPOINT)


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
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    error_budget: int = CONSECUTIVE_ERROR_BUDGET,
    iterations: int | None = None,
) -> int:
    """Drive ``endpoint`` at ~1 qps with the synthetic header.

    Args:
        endpoint: full HTTP URL for the dual-read endpoint.
        sample_keys: M3 corpus claim ids — round-robined across queries.
        interval_seconds: delay between requests.
        error_budget: consecutive-error threshold; on exhaustion the
            function returns ``75`` (EX_TEMPFAIL) so systemd
            ``Restart=always`` re-launches a fresh process.
        iterations: if provided, exit after this many requests (test hook).

    Returns:
        process exit code: ``0`` on completion (only possible when
        ``iterations`` is set), ``75`` on error-budget exhaustion.
    """
    if not sample_keys:
        return 2
    client = httpx.Client(timeout=5.0, headers=HEADERS)
    consecutive_errors = 0
    idx = 0
    sent = 0
    try:
        while True:
            key = sample_keys[idx % len(sample_keys)]
            idx += 1
            try:
                response = client.get(f"{endpoint}?key={key}")
                LOG.info("synth_qry key=%s status=%d", key, response.status_code)
                consecutive_errors = 0
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(name)s %(message)s",
    )
    endpoint = _resolve_endpoint()
    fixture_path = _resolve_fixture_path()
    sample_keys = _load_fixture(fixture_path)
    LOG.info(
        "synth loader starting endpoint=%s fixture=%s keys=%d",
        endpoint,
        fixture_path,
        len(sample_keys),
    )
    return run_loader(
        endpoint=endpoint,
        sample_keys=sample_keys,
        interval_seconds=_interval_seconds(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
