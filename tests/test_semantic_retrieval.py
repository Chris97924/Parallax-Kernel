"""Tests for M8 semantic retrieval core (embeddings + fusion).

All offline — the DeterministicStubProvider backs every test; the Ollama
provider is exercised only via a monkeypatched httpx so no live call fires.
"""

from __future__ import annotations

import math

import pytest

from parallax.retrieval import embeddings as emb
from parallax.retrieval.embeddings import (
    BGE_M3_DIM,
    EMBEDDING_BASE_URL_ENV,
    DeterministicStubProvider,
    EmbeddingError,
    OllamaEmbeddingProvider,
    get_embedding_provider,
    has_live_embedding_provider,
)
from parallax.retrieval.semantic import (
    RRF_K_DEFAULT,
    cosine,
    dense_rank,
    hybrid_rank,
    lexical_rank,
    rrf_merge,
    tokenize,
)

# ---------------------------------------------------------------------------
# cosine
# ---------------------------------------------------------------------------


def test_cosine_identical_is_one():
    v = [0.3, 0.4, 0.5]
    assert cosine(v, v) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero_not_nan():
    out = cosine([0.0, 0.0], [1.0, 2.0])
    assert out == 0.0
    assert not math.isnan(out)


def test_cosine_symmetry():
    a = [1.0, 2.0, 3.0]
    b = [4.0, 5.0, 6.0]
    assert cosine(a, b) == pytest.approx(cosine(b, a))


def test_cosine_length_mismatch_raises():
    with pytest.raises(ValueError):
        cosine([1.0, 2.0], [1.0])


# ---------------------------------------------------------------------------
# tokenize / lexical_rank
# ---------------------------------------------------------------------------


def test_tokenize_strips_punctuation_and_lowercases():
    assert tokenize("Teal, please?") == ["teal", "please"]


def test_lexical_rank_prefers_overlap():
    texts = ["nothing relevant here", "the cat likes tennis and coffee"]
    ranked = lexical_rank("tennis coffee", texts)
    assert ranked[0] == 1


def test_lexical_rank_empty_query_returns_input_order():
    texts = ["a", "b", "c"]
    assert lexical_rank("", texts) == [0, 1, 2]


def test_lexical_rank_idf_favors_rare_term():
    # "common" appears in every doc; "rare" in only one. A query with both
    # should rank the doc holding the rare term first.
    texts = [
        "common common common",
        "common rare",
        "common common",
    ]
    ranked = lexical_rank("common rare", texts)
    assert ranked[0] == 1


# ---------------------------------------------------------------------------
# rrf_merge
# ---------------------------------------------------------------------------


def test_rrf_merge_known_fusion():
    # list A: [0, 1, 2], list B: [2, 1, 0]. Index 1 is rank-2 in both;
    # indices 0 and 2 are each rank-1 once and rank-3 once. So 0,1,2 all tie?
    # 0: 1/61 + 1/63 ; 1: 1/62 + 1/62 ; 2: 1/63 + 1/61. 0 and 2 equal; 1 lower.
    k = RRF_K_DEFAULT
    s0 = 1 / (k + 1) + 1 / (k + 3)
    s1 = 1 / (k + 2) + 1 / (k + 2)
    s2 = 1 / (k + 3) + 1 / (k + 1)
    assert s0 == pytest.approx(s2)
    assert s1 < s0
    merged = rrf_merge([[0, 1, 2], [2, 1, 0]])
    # 0 and 2 tie on score; tie falls back to ascending index → 0 before 2,
    # then 1 last.
    assert merged == [0, 2, 1]


def test_rrf_merge_single_list_passthrough():
    assert rrf_merge([[2, 0, 1]]) == [2, 0, 1]


def test_rrf_merge_index_in_one_list_only():
    # index 3 appears only in the second list — still included.
    merged = rrf_merge([[0, 1], [3, 0]])
    assert set(merged) == {0, 1, 3}


def test_rrf_merge_tie_break_overrides_index():
    # Two indices with identical fused score; tie_break makes the higher index win.
    tie_break = [10.0, 0.0]  # index 1 has lower tie_break → wins ties
    merged = rrf_merge([[0], [1]], tie_break=tie_break)
    assert merged == [1, 0]


def test_rrf_merge_empty():
    assert rrf_merge([]) == []
    assert rrf_merge([[], []]) == []


# ---------------------------------------------------------------------------
# DeterministicStubProvider
# ---------------------------------------------------------------------------


def test_stub_provider_deterministic():
    p = DeterministicStubProvider()
    a = p.embed(["hello world"])[0]
    b = p.embed(["hello world"])[0]
    assert a == b


def test_stub_provider_distinct_texts_differ():
    p = DeterministicStubProvider()
    a = p.embed(["alpha"])[0]
    b = p.embed(["beta"])[0]
    assert a != b


def test_stub_provider_normalized_and_dim():
    p = DeterministicStubProvider(dim=32)
    vec = p.embed(["something"])[0]
    assert len(vec) == 32
    assert math.sqrt(sum(x * x for x in vec)) == pytest.approx(1.0)


def test_stub_provider_rejects_bad_dim():
    with pytest.raises(ValueError):
        DeterministicStubProvider(dim=0)


def test_l2_normalize_zero_vector_is_passthrough():
    # A zero vector has no direction; normalization returns it unchanged
    # rather than dividing by zero.
    assert emb._l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# dense_rank / hybrid_rank
# ---------------------------------------------------------------------------


class _NearProvider:
    """A toy provider where the query is closest to a chosen target index."""

    dim = 3
    id = "near-test"

    def __init__(self, vectors: dict[str, list[float]]):
        self._v = vectors

    def embed(self, texts):
        return [self._v[t] for t in texts]


def test_dense_rank_orders_by_cosine():
    vecs = {
        "Q": [1.0, 0.0, 0.0],
        "far": [0.0, 1.0, 0.0],
        "near": [0.9, 0.1, 0.0],
    }
    provider = _NearProvider(vecs)
    ranked = dense_rank("Q", ["far", "near"], provider)
    assert ranked[0] == 1  # "near" wins


def test_dense_rank_empty():
    assert dense_rank("Q", [], DeterministicStubProvider()) == []


def test_hybrid_rank_dense_surfaces_non_lexical_match():
    # The lexically-stronger doc (shares the word "cat") is dense-far; the
    # semantically-near doc has zero lexical overlap. Dense ranks index 1
    # first, lexical ranks index 0 first. RRF with both at rank-1/rank-2
    # ties — so the dense signal must be enough to keep the near doc on top
    # of fusion when lexical alone would not have surfaced it. We assert the
    # near doc is NOT buried (i.e. it is fused into the top, beating a doc the
    # dense path scored as orthogonal).
    texts = ["cat trivia unrelated", "feline companion", "weather forecast"]
    vecs = {
        "cat": [1.0, 0.0, 0.0],
        "cat trivia unrelated": [0.0, 1.0, 0.0],  # lexical hit, dense-far
        "feline companion": [0.95, 0.05, 0.0],  # no lexical hit, dense-near
        "weather forecast": [0.0, 0.0, 1.0],  # neither
    }
    provider = _NearProvider(vecs)
    ranked = hybrid_rank("cat", texts, provider)
    # feline (dense-near, index 1) must outrank the orthogonal weather doc and
    # land in the top-2 of fusion — proof the dense leg contributes signal.
    assert 1 in ranked[:2]
    assert ranked.index(1) < ranked.index(2)


def test_hybrid_rank_lexical_exact_match_survives_fusion():
    texts = ["the user likes tennis", "weather report"]
    vecs = {
        "tennis": [1.0, 0.0],
        "the user likes tennis": [0.0, 1.0],  # dense-far on purpose
        "weather report": [0.0, 1.0],
    }
    provider = _NearProvider(vecs)
    ranked = hybrid_rank("tennis", texts, provider)
    assert ranked[0] == 0  # lexical exact match still wins fusion


def test_hybrid_rank_degrades_to_lexical_on_embedding_error():
    class _BoomProvider:
        dim = 3
        id = "boom"

        def embed(self, texts):
            raise EmbeddingError("server down")

    texts = ["irrelevant", "tennis match recap"]
    ranked = hybrid_rank("tennis", texts, _BoomProvider())
    # No crash; falls back to lexical-only ordering (tennis doc first).
    assert ranked[0] == 1


def test_hybrid_rank_dense_overturns_a_lexical_distractor():
    # M8 quality contract at the fusion level: the answer-bearing doc (index 1)
    # is lexically WEAK — it shares no query token — but semantically STRONG
    # (dense rank 1). A lexical DISTRACTOR (index 0) shares two high-IDF query
    # tokens in an unrelated sense, so lexical-only ranks IT first and buries
    # the answer.
    #
    # The distractor is lexical rank 1 + dense rank 2; the answer is lexical
    # rank 2 + dense rank 1. Symmetric 2-list RRF *ties* them — which is exactly
    # why the eval store passes a ``tie_break`` (by vault_path) so the tie
    # resolves toward the answer. Here we mirror that production call: the
    # answer carries the lowest tie_break, and hybrid must surface it above the
    # distractor it lost to under pure lexical scoring.
    query = "kernel memory model"
    texts = [
        "kernel memory popcorn jar",  # 0 distractor: 2 lexical hits, dense-far
        "rust borrow checker ownership",  # 1 ANSWER: 0 lexical hits, dense-near
        "unrelated weather forecast",  # 2 noise
    ]
    vecs = {
        "kernel memory model": [1.0, 0.0, 0.0],
        "kernel memory popcorn jar": [0.0, 1.0, 0.0],  # lexical hit, dense-far
        "rust borrow checker ownership": [0.97, 0.05, 0.0],  # dense-near, no lexical
        "unrelated weather forecast": [0.0, 0.0, 1.0],  # neither
    }
    provider = _NearProvider(vecs)

    lexical_only = lexical_rank(query, texts)
    assert lexical_only.index(0) < lexical_only.index(1)  # distractor buries answer

    # tie_break: the answer (index 1) wins ties — same contract the store gives
    # the answer row via its ascending vault_path position.
    tie_break = [1.0, 0.0, 2.0]
    ranked = hybrid_rank(query, texts, provider, tie_break=tie_break)
    assert ranked[0] == 1  # answer surfaces to the top under fusion
    assert ranked.index(1) < ranked.index(0)  # answer now beats the distractor


def test_hybrid_rank_empty_texts():
    assert hybrid_rank("q", [], DeterministicStubProvider()) == []


# ---------------------------------------------------------------------------
# OllamaEmbeddingProvider (httpx mocked — no live call)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def test_ollama_provider_request_shape_and_parse(monkeypatch):
    import httpx

    captured = {}

    def _fake_post(url, json, timeout):  # noqa: A002 - mirrors httpx.post signature
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse({"embeddings": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = OllamaEmbeddingProvider(model="bge-m3", base_url="http://gb10:11434", dim=3)
    out = provider.embed(["hello"])
    assert out == [[0.1, 0.2, 0.3]]
    # Modern Ollama: POST /api/embed with a batched `input` list, response
    # `embeddings` is a list-of-vectors in input order.
    assert captured["url"] == "http://gb10:11434/api/embed"
    assert captured["json"] == {"model": "bge-m3", "input": ["hello"]}


def test_ollama_provider_batch_yields_vectors_in_order(monkeypatch):
    import httpx

    captured = {}

    def _fake_post(url, json, timeout):  # noqa: A002
        captured["json"] = json
        # One request for the whole batch; vectors returned in input order.
        return _FakeResponse({"embeddings": [[1.0, 0.0], [0.0, 2.0], [3.0, 3.0]]})

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434", dim=2)
    out = provider.embed(["a", "b", "c"])
    assert out == [[1.0, 0.0], [0.0, 2.0], [3.0, 3.0]]
    # Single batched call carries every input.
    assert captured["json"] == {"model": "bge-m3", "input": ["a", "b", "c"]}


def test_ollama_provider_empty_texts_skips_http(monkeypatch):
    import httpx

    def _boom(*args, **kwargs):
        raise AssertionError("no HTTP call should fire for empty input")

    monkeypatch.setattr(httpx, "post", _boom)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434")
    assert provider.embed([]) == []


def test_ollama_provider_length_mismatch_raises_embedding_error(monkeypatch):
    import httpx

    def _fake_post(url, json, timeout):  # noqa: A002
        # Two inputs but only one vector back — must not silently truncate.
        return _FakeResponse({"embeddings": [[0.1, 0.2, 0.3]]})

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434")
    with pytest.raises(EmbeddingError):
        provider.embed(["x", "y"])


def test_ollama_provider_http_error_raises_embedding_error(monkeypatch):
    import httpx

    def _fake_post(url, json, timeout):  # noqa: A002
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434")
    with pytest.raises(EmbeddingError):
        provider.embed(["x"])


def test_ollama_provider_bad_shape_raises_embedding_error(monkeypatch):
    import httpx

    def _fake_post(url, json, timeout):  # noqa: A002
        return _FakeResponse({"not_embeddings": []})

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434")
    with pytest.raises(EmbeddingError):
        provider.embed(["x"])


def test_ollama_provider_empty_vector_raises_embedding_error(monkeypatch):
    import httpx

    def _fake_post(url, json, timeout):  # noqa: A002
        # Right count, but an empty vector is not a usable embedding.
        return _FakeResponse({"embeddings": [[]]})

    monkeypatch.setattr(httpx, "post", _fake_post)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434")
    with pytest.raises(EmbeddingError):
        provider.embed(["x"])


def test_ollama_provider_strips_trailing_slash():
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434/")
    assert provider.base_url == "http://gb10:11434"


def test_ollama_provider_missing_httpx_raises_embedding_error(monkeypatch):
    # In a core install httpx is absent (it lives in the [dev]/[extract] extras).
    # The lazy import failure must convert to EmbeddingError so hybrid_rank
    # degrades to lexical-only instead of crashing.
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("No module named 'httpx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434")
    with pytest.raises(EmbeddingError):
        provider.embed(["x"])


def test_hybrid_rank_degrades_when_httpx_missing(monkeypatch):
    # End-to-end: a missing httpx through the live provider must let
    # hybrid_rank fall back to lexical-only (no crash).
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("No module named 'httpx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    provider = OllamaEmbeddingProvider(base_url="http://gb10:11434")
    texts = ["irrelevant", "tennis match recap"]
    ranked = hybrid_rank("tennis", texts, provider)
    assert ranked[0] == 1  # lexical-only ordering survives


# ---------------------------------------------------------------------------
# get_embedding_provider factory
# ---------------------------------------------------------------------------


def test_embedding_base_url_env_constant_value():
    # The public constant must name the documented env var so tests and callers
    # reference it without hard-coding the literal string.
    assert EMBEDDING_BASE_URL_ENV == "PARALLAX_EMBEDDING_BASE_URL"


def test_factory_returns_stub_without_base_url(monkeypatch):
    monkeypatch.delenv(EMBEDDING_BASE_URL_ENV, raising=False)
    provider = get_embedding_provider()
    assert isinstance(provider, DeterministicStubProvider)


def test_has_live_provider_false_without_base_url(monkeypatch):
    monkeypatch.delenv("PARALLAX_EMBEDDING_BASE_URL", raising=False)
    assert has_live_embedding_provider() is False


def test_has_live_provider_true_with_base_url(monkeypatch):
    monkeypatch.setenv("PARALLAX_EMBEDDING_BASE_URL", "http://gb10:11434")
    assert has_live_embedding_provider() is True


def test_has_live_provider_false_with_empty_base_url(monkeypatch):
    monkeypatch.setenv("PARALLAX_EMBEDDING_BASE_URL", "   ")
    assert has_live_embedding_provider() is False


def test_factory_returns_ollama_with_base_url(monkeypatch):
    monkeypatch.setenv("PARALLAX_EMBEDDING_BASE_URL", "http://gb10:11434")
    monkeypatch.setenv("PARALLAX_EMBEDDING_MODEL", "bge-m3")
    provider = get_embedding_provider()
    assert isinstance(provider, OllamaEmbeddingProvider)
    assert provider.base_url == "http://gb10:11434"
    assert provider.model == "bge-m3"
    assert provider.dim == BGE_M3_DIM


def test_factory_default_model_when_unset(monkeypatch):
    monkeypatch.setenv("PARALLAX_EMBEDDING_BASE_URL", "http://gb10:11434")
    monkeypatch.delenv("PARALLAX_EMBEDDING_MODEL", raising=False)
    provider = get_embedding_provider()
    assert provider.model == emb.DEFAULT_EMBEDDING_MODEL
