"""M3-T1.2 — AphelionReadAdapter stub for US-011 dual-read (M3a).

STUB — always raises ``AphelionUnreachableError("not_implemented")``.

The real HTTP client adapter that actually calls the Aphelion API lands in
M3b / M4.  This stub exists so ``DualReadRouter`` has a concrete secondary
``QueryPort`` to wrap without requiring the real Aphelion client.  Production
deployment keeps ``DUAL_READ=false`` until the real adapter ships.

Conforms to ``parallax.router.ports.QueryPort`` (runtime-checkable Protocol).
"""

from __future__ import annotations

from parallax.router.contracts import QueryRequest, RetrievalEvidence

__all__ = ["AphelionReadAdapter", "AphelionUnreachableError"]


class AphelionUnreachableError(Exception):
    """Raised when the Aphelion secondary cannot be reached or is not implemented.

    ``reason`` is a short tag used for outcome classification in DualReadRouter:
      - ``"not_implemented"`` — stub; real adapter not yet wired (M3a)
      - ``"timeout"``         — secondary exceeded secondary_timeout_ms
      - ``"connection_error"`` — network/transport failure
    """

    def __init__(self, reason: str) -> None:
        """Initialise with a short reason tag for outcome classification."""
        super().__init__(f"Aphelion unreachable: {reason}")
        self.reason = reason


class AphelionReadAdapter:
    """Stub QueryPort adapter for Aphelion secondary reads.

    M3a: ``query()`` always raises ``AphelionUnreachableError("not_implemented")``.
    The real implementation (HTTP client + auth) lands in M3b/M4.

    ``timeout_ms`` is the spec'd 100ms shadow timeout (ralplan §3 line 251).
    It is stored for use by the real adapter; the stub ignores it.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_ms: float = 100.0,
    ) -> None:
        """Initialise the stub adapter with optional base URL and timeout.

        Args:
            base_url: Aphelion API base URL (stored for real adapter; unused by stub).
            timeout_ms: Shadow timeout in milliseconds (default 100ms).
        """
        self._base_url = base_url
        self._timeout_ms = timeout_ms

    def query(self, request: QueryRequest) -> RetrievalEvidence:
        """Raise AphelionUnreachableError; real adapter arrives in M3b/M4."""
        raise AphelionUnreachableError("not_implemented")
