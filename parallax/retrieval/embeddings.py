"""Embedding providers for M8 semantic retrieval.

Two backends speak the :class:`EmbeddingProvider` protocol:

* :class:`DeterministicStubProvider` — pure-stdlib, hash-seeded vectors. No
  network. Used by every unit test and as the offline default so importing
  this module never makes a network call.
* :class:`OllamaEmbeddingProvider` — ``httpx`` against an Ollama server serving
  ``bge-m3`` (1024-dim). Base URL is operator config (env, default GB10).

:func:`get_embedding_provider` is the env-driven factory: it returns the stub
unless ``PARALLAX_EMBEDDING_BASE_URL`` selects the live Ollama provider.

``httpx`` is imported lazily inside :class:`OllamaEmbeddingProvider` so the
stub path (and module import) carries no dependency on it being installed.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import threading
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

__all__ = [
    "EmbeddingProvider",
    "EmbeddingError",
    "DeterministicStubProvider",
    "OllamaEmbeddingProvider",
    "get_embedding_provider",
    "has_live_embedding_provider",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_EMBEDDING_MODEL",
    "EMBEDDING_BASE_URL_ENV",
    "STUB_DIM",
    "BGE_M3_DIM",
]

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_BASE_URL = "http://192.168.1.134:11434"
DEFAULT_EMBEDDING_MODEL = "bge-m3"
STUB_DIM = 64
BGE_M3_DIM = 1024


class EmbeddingError(RuntimeError):
    """Raised when an embedding backend cannot produce vectors.

    Callers degrade to lexical-only on this error — a retrieval must never
    crash because the embedding server is unreachable.
    """


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embeds text into fixed-dimension float vectors.

    ``id`` is a stable identity used in the per-call embedding cache key so two
    providers never share cached vectors. ``dim`` is the output dimensionality.
    """

    dim: int
    id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in order. May raise EmbeddingError."""
        ...


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class DeterministicStubProvider:
    """Hash-seeded deterministic embeddings — no network, stdlib only.

    Same text → identical unit vector; different texts → different vectors.
    Useful for tests and as the offline fallback when the flag is on but no
    live provider is configured.
    """

    def __init__(self, *, dim: int = STUB_DIM) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self.id = f"stub-{dim}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        seed_bytes = hashlib.blake2b((text or "").encode("utf-8"), digest_size=8).digest()
        rng = random.Random(int.from_bytes(seed_bytes, "big"))
        vec = [rng.gauss(0.0, 1.0) for _ in range(self.dim)]
        return _l2_normalize(vec)


class OllamaEmbeddingProvider:
    """Embeddings from an Ollama server (default model ``bge-m3``, 1024-dim).

    ``base_url`` is caller/operator-controlled and **not validated**. Do not
    wire it to untrusted configuration — a malicious value could redirect the
    request at an internal service (SSRF). Pin it in deployment code.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_EMBEDDING_MODEL,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        dim: int = BGE_M3_DIM,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dim = dim
        self.timeout = timeout
        self.id = f"ollama:{model}@{self.base_url}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        # One batched request: modern Ollama POST /api/embed accepts a list of
        # inputs and returns them in input order. This both speaks the current
        # API (legacy /api/embeddings would 404 on a current server) and removes
        # the O(rows) per-text HTTP round-trips of the old per-row loop.
        inputs = list(texts)
        if not inputs:
            return []

        # Lazy import: the stub path never imports httpx, and httpx lives in the
        # [dev]/[extract] extras — not core deps. In a core install a missing
        # httpx must surface as EmbeddingError so callers (hybrid_rank) degrade
        # to lexical-only instead of crashing.
        try:
            import httpx  # type: ignore[import]
        except ImportError as exc:
            logger.warning("httpx unavailable for Ollama embeddings: %s", exc)
            raise EmbeddingError("httpx not installed (pip install '.[extract]')") from exc

        payload = {"model": self.model, "input": inputs}
        try:
            resp = httpx.post(
                f"{self.base_url}/api/embed",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Ollama embedding request failed: %s", exc)
            raise EmbeddingError(str(exc)) from exc

        embeddings = body.get("embeddings") if isinstance(body, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != len(inputs):
            raise EmbeddingError(
                f"Ollama response 'embeddings' missing or wrong length "
                f"(expected {len(inputs)}): {body!r:.120}"
            )
        vectors: list[list[float]] = []
        for vec in embeddings:
            if not isinstance(vec, list) or not vec:
                raise EmbeddingError(f"Ollama response vector empty or not a list: {body!r:.120}")
            vectors.append([float(x) for x in vec])
        return vectors


_FACTORY_LOCK = threading.Lock()

#: Env var selecting the live Ollama provider (non-empty value enables it).
#: Public so tests and callers reference the name without hard-coding the string.
EMBEDDING_BASE_URL_ENV = "PARALLAX_EMBEDDING_BASE_URL"
# Backward-compatible private alias (pre-existing internal references).
_EMBEDDING_BASE_URL_ENV = EMBEDDING_BASE_URL_ENV


def has_live_embedding_provider() -> bool:
    """Return True when a live Ollama URL is configured in the environment.

    Use this to guard code that should not run stub-backed dense retrieval in
    production. When False, callers should degrade to lexical-only rather than
    fusing hash-random stub vectors into ranked results.

    Env:
        PARALLAX_EMBEDDING_BASE_URL  Non-empty value selects the live path.
    """
    return bool(os.environ.get(_EMBEDDING_BASE_URL_ENV, "").strip())


def get_embedding_provider() -> EmbeddingProvider:
    """Build a provider from the environment.

    Returns :class:`OllamaEmbeddingProvider` when ``PARALLAX_EMBEDDING_BASE_URL``
    is set (live path); otherwise the deterministic stub. Importing this module
    or calling this factory never makes a network call — the Ollama provider
    defers the HTTP client import until ``embed`` is invoked.

    Env:
        PARALLAX_EMBEDDING_BASE_URL  Ollama base URL (selects the live path).
        PARALLAX_EMBEDDING_MODEL     model name (default ``bge-m3``).
    """
    base_url = os.environ.get(_EMBEDDING_BASE_URL_ENV, "").strip()
    if not base_url:
        return DeterministicStubProvider()
    model = os.environ.get("PARALLAX_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip()
    with _FACTORY_LOCK:
        return OllamaEmbeddingProvider(model=model or DEFAULT_EMBEDDING_MODEL, base_url=base_url)
