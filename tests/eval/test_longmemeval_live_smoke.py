"""Live bge-m3 smoke — skipped by default; never runs in CI.

Hits the GB10 Ollama server to confirm the real embedding path returns a
1024-dim vector. Guarded by an env switch so default ``pytest`` collection
skips it (no GB10 dependency for the gate). Marked ``integration`` per the
existing marker in ``pyproject.toml``.

Run explicitly::

    PARALLAX_EMBEDDING_LIVE=1 \\
    PARALLAX_EMBEDDING_BASE_URL=http://192.168.1.134:11434 \\
    pytest tests/eval/test_longmemeval_live_smoke.py -m integration
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

_LIVE = os.environ.get("PARALLAX_EMBEDDING_LIVE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


@pytest.mark.skipif(not _LIVE, reason="set PARALLAX_EMBEDDING_LIVE=1 to hit GB10 bge-m3")
def test_bge_m3_returns_1024_dim_vector():
    from parallax.retrieval.embeddings import BGE_M3_DIM, OllamaEmbeddingProvider

    base_url = os.environ.get(
        "PARALLAX_EMBEDDING_BASE_URL", "http://192.168.1.134:11434"
    )
    provider = OllamaEmbeddingProvider(base_url=base_url)
    vecs = provider.embed(["the user's favourite colour is teal"])
    assert len(vecs) == 1
    assert len(vecs[0]) == BGE_M3_DIM
    assert any(abs(x) > 0 for x in vecs[0])
