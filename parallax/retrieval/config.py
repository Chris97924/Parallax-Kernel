"""Feature-flag and env readers for M8 semantic retrieval.

Kept separate from the frozen :class:`parallax.config.ParallaxConfig` so the
M8 knobs do not widen that snapshot. These are read at call time (env is the
source of truth) so the flag can be flipped without rebuilding config.

Flag: ``PARALLAX_SEMANTIC_RETRIEVAL`` (default OFF). When OFF, callers take
their pre-existing lexical path verbatim — zero behavior change.
"""

from __future__ import annotations

import os

__all__ = ["SEMANTIC_RETRIEVAL_ENV", "semantic_retrieval_enabled"]

SEMANTIC_RETRIEVAL_ENV = "PARALLAX_SEMANTIC_RETRIEVAL"

# Same truthy set as parallax.config so the flag parses consistently.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def semantic_retrieval_enabled() -> bool:
    """True iff the M8 hybrid retrieval flag is enabled in the environment."""
    return os.environ.get(SEMANTIC_RETRIEVAL_ENV, "").strip().lower() in _TRUTHY
