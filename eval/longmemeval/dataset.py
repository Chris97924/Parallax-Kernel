"""LongMemEval dataset loader.

Each question in the JSON is a dict with these keys:

* ``question_id``    — unique id (e.g. ``gpt4_2655b836``)
* ``question_type``  — one of 5 categories (temporal-reasoning, multi-session, ...)
* ``question``       — the user's question
* ``answer``         — gold answer string
* ``question_date``  — timestamp when the question was asked
* ``haystack_dates`` — list[str] session timestamps (aligned with sessions)
* ``haystack_session_ids`` — list[str] session identifiers
* ``haystack_sessions`` — list[list[turn]] where turn = {role, content, has_answer}
* ``answer_session_ids`` — optional, gold session ids (may be None)

We expose a ``Question`` NamedTuple so the rest of the harness never touches
raw dict keys.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple


class Turn(NamedTuple):
    role: str
    content: str
    has_answer: bool


class Session(NamedTuple):
    session_id: str
    date: str
    turns: tuple[Turn, ...]


class Question(NamedTuple):
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    sessions: tuple[Session, ...]
    answer_session_ids: tuple[str, ...]


def _parse_turn(raw: dict) -> Turn:
    return Turn(
        role=str(raw["role"]),
        content=str(raw["content"]),
        has_answer=bool(raw.get("has_answer", False)),
    )


def _parse_question(raw: dict) -> Question:
    dates = raw.get("haystack_dates") or []
    sids = raw.get("haystack_session_ids") or []
    sessions_raw = raw.get("haystack_sessions") or []
    sessions = tuple(
        Session(
            session_id=str(sids[i]) if i < len(sids) else f"s{i}",
            date=str(dates[i]) if i < len(dates) else "",
            turns=tuple(_parse_turn(t) for t in sessions_raw[i]),
        )
        for i in range(len(sessions_raw))
    )
    ans_sids = raw.get("answer_session_ids") or ()
    return Question(
        question_id=str(raw["question_id"]),
        question_type=str(raw["question_type"]),
        question=str(raw["question"]),
        answer=str(raw["answer"]),
        question_date=str(raw.get("question_date", "")),
        sessions=sessions,
        answer_session_ids=tuple(str(x) for x in ans_sids) if ans_sids else (),
    )


def load_dataset(path: str | Path) -> list[Question]:
    """Load a LongMemEval split JSON into a list of Question."""
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"expected list, got {type(data).__name__}")
    return [_parse_question(q) for q in data]


def iter_questions(
    path: str | Path, limit: int | None = None, types: frozenset[str] | None = None
) -> Iterator[Question]:
    """Stream questions with optional type filter + count limit."""
    yielded = 0
    for q in load_dataset(path):
        if types is not None and q.question_type not in types:
            continue
        yield q
        yielded += 1
        if limit is not None and yielded >= limit:
            return
