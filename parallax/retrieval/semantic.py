"""Hybrid lexical + dense retrieval fused with Reciprocal Rank Fusion.

M8 core. Pure-Python (stdlib ``math`` only — no numpy), so it runs in the same
env the rest of the package does (numpy is not a core dependency).

Pipeline:

1. ``lexical_rank``  — token-overlap (IDF-lite) ranking of candidate texts.
2. ``dense_rank``    — cosine of query vs candidate embeddings from an
   :class:`~parallax.retrieval.embeddings.EmbeddingProvider`.
3. ``rrf_merge``     — fuse the two ranked lists, ``score = Σ 1/(k+rank)``,
   ``k = 60`` (Cormack et al. / Perihelion v0.4 default).

``hybrid_rank`` is the entrypoint: it returns candidate *indices* in fused
order, leaving the caller to apply its own top_k / budget / chronological
re-sort. A dense-side failure degrades to lexical-only (never raises out).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence

from parallax.retrieval.embeddings import EmbeddingError, EmbeddingProvider

__all__ = [
    "RRF_K_DEFAULT",
    "cosine",
    "tokenize",
    "lexical_rank",
    "dense_rank",
    "rrf_merge",
    "hybrid_rank",
]

logger = logging.getLogger(__name__)

RRF_K_DEFAULT: int = 60

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lowercase word-boundary tokenizer (matches the eval store semantics)."""
    return _WORD_RE.findall((text or "").lower())


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors. 0.0 for a zero vector."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def lexical_rank(query: str, texts: Sequence[str]) -> list[int]:
    """Rank candidate indices by IDF-lite token-overlap score, descending.

    Score: sum over query tokens present in a candidate of ``idf(token)``,
    normalized by the query token count. IDF dampens common tokens so a rare
    shared term outweighs a frequent one. Ties keep input order (stable sort),
    so the caller's own tie-break key governs final ordering after fusion.
    """
    q_tokens = set(tokenize(query))
    if not q_tokens:
        return list(range(len(texts)))

    doc_tokens = [set(tokenize(t)) for t in texts]
    n_docs = len(texts) or 1
    df: dict[str, int] = {}
    for toks in doc_tokens:
        for tok in toks & q_tokens:
            df[tok] = df.get(tok, 0) + 1

    def _idf(tok: str) -> float:
        # +1 smoothing so a token in every doc still contributes a little.
        return math.log((n_docs + 1) / (df.get(tok, 0) + 1)) + 1.0

    scores: list[tuple[int, float]] = []
    denom = float(len(q_tokens))
    for i, toks in enumerate(doc_tokens):
        overlap = toks & q_tokens
        score = sum(_idf(tok) for tok in overlap) / denom
        scores.append((i, score))

    scores.sort(key=lambda it: it[1], reverse=True)
    return [i for i, _ in scores]


def dense_rank(
    query: str,
    texts: Sequence[str],
    provider: EmbeddingProvider,
) -> list[int]:
    """Rank candidate indices by cosine(query, candidate) embedding, descending.

    Raises :class:`EmbeddingError` if the provider fails; callers that want
    graceful degradation should catch it (``hybrid_rank`` does).
    """
    if not texts:
        return []
    vectors = provider.embed([query, *texts])
    q_vec = vectors[0]
    item_vecs = vectors[1:]
    scored = [(i, cosine(q_vec, item_vecs[i])) for i in range(len(item_vecs))]
    scored.sort(key=lambda it: it[1], reverse=True)
    return [i for i, _ in scored]


def rrf_merge(
    ranked_lists: Sequence[Sequence[int]],
    *,
    k: int = RRF_K_DEFAULT,
    tie_break: Sequence[float] | None = None,
) -> list[int]:
    """Fuse ranked index lists with Reciprocal Rank Fusion.

    ``score(d) = Σ_lists 1 / (k + rank_list(d))`` with 1-based ranks. An index
    absent from a list contributes nothing from that list. Output is all
    distinct indices sorted by fused score descending.

    ``tie_break`` (optional, one value per index, lower wins) breaks equal
    fused scores deterministically; without it, ties fall back to ascending
    index so reruns are always bit-identical.
    """
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, idx in enumerate(ranked, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)

    def _sort_key(idx: int) -> tuple[float, float, int]:
        tb = tie_break[idx] if tie_break is not None else 0.0
        return (-scores[idx], tb, idx)

    return sorted(scores.keys(), key=_sort_key)


def hybrid_rank(
    query: str,
    texts: Sequence[str],
    provider: EmbeddingProvider,
    *,
    k: int = RRF_K_DEFAULT,
    tie_break: Sequence[float] | None = None,
) -> list[int]:
    """Hybrid lexical + dense ranking fused with RRF.

    Returns candidate indices in fused order. If the dense side fails
    (embedding server unreachable), logs and degrades to lexical-only — a
    retrieval must never crash because GB10 is offline.
    """
    if not texts:
        return []
    lex = lexical_rank(query, texts)
    try:
        dense = dense_rank(query, texts, provider)
    except EmbeddingError as exc:
        logger.warning("dense rank unavailable, lexical-only: %s", exc)
        return lex
    return rrf_merge([lex, dense], k=k, tie_break=tie_break)
