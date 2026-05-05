"""Live cross-store arbitration contract (M3b Phase 2 — US-004-M3-T2.1).

This module is intentionally separate from
``parallax.router.contracts.ArbitrationDecision`` (which arbitrates
Crosswalk field-mapping state during backfill — semantics collide). The
contract here is the *runtime* arbitration verdict produced after a
DualReadRouter dispatch: for a given query, did Parallax or Aphelion
"win"?  This is the narrow, source-level rule table from PRD addendum
Q1 Option A (RECENT/ARTIFACT/CHANGE_TRACE/TEMPORAL → parallax,
ENTITY_PROFILE → aphelion).

Design pinning notes:

- ``LiveArbitrationDecision`` is a frozen dataclass; ``arbitrate`` is a
  pure function with no I/O and no side-effects.
- ``policy_version`` defaults to the current RC string (``v0.3.0-rc``).
  Old serialized lines may have been written before this field existed;
  on read, missing keys coerce to ``POLICY_VERSION_PRE_RC`` so the
  decoder is robust to historical data without ever raising.
- ``to_json_line`` uses ``json.dumps(..., sort_keys=True)`` for
  byte-deterministic output across runs.
- ``reason_code`` format: ``"source-level/{query_type}/{outcome}"``.
  Stable across calls with identical inputs (KISS — the spec did not
  mandate a richer schema).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Literal

from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.types import QueryType

__all__ = [
    "POLICY_VERSION_PRE_RC",
    "POLICY_VERSION_DEFAULT",
    "LiveArbitrationDecision",
    "arbitrate",
]

# Sentinel value used when reading a serialized line that pre-dates the
# ``policy_version`` field.  Reader-side robustness — never written.
POLICY_VERSION_PRE_RC = "v0.0-pre-rc"

# Default policy version emitted by ``arbitrate`` for new decisions.
POLICY_VERSION_DEFAULT = "v0.3.0-rc"

# Source-level rule table: which store "owns" each QueryType when both
# sides return populated results (Q1 Option A).  Crosswalk-miss
# (secondary is None or has no hits) overrides this and always resolves
# to ``"fallback"``.
_QT_OWNERSHIP: dict[QueryType, Literal["parallax", "aphelion"]] = {
    QueryType.RECENT_CONTEXT: "parallax",
    QueryType.ARTIFACT_CONTEXT: "parallax",
    QueryType.CHANGE_TRACE: "parallax",
    QueryType.TEMPORAL_CONTEXT: "parallax",
    QueryType.ENTITY_PROFILE: "aphelion",
}


WinningSource = Literal["parallax", "aphelion", "tie", "fallback"]


@dataclass(frozen=True)
class LiveArbitrationDecision:
    """單次即時跨儲存查詢的不可變仲裁判決。

    封裝 DualReadRouter 派送後的仲裁結果，記錄哪個儲存端勝出
    以及相關的元資料資訊。

    Attributes
    ----------
    winning_source : WinningSource
        勝出的資料來源，為 "parallax"、"aphelion"、"tie" 或 "fallback" 其一。
    tie_breaker_rule : str
        所套用規則的簡短識別碼（例如 "source-level" 代表 Q1 Option A 規則表）。
    conflict_event_id : str or None
        衝突事件紀錄的識別碼（對應 Story 5），無衝突事件時為 None。
    policy_version : str
        產生此判決的規則表版本字串，恆為非 null。
    correlation_id : str
        關聯至觸發本次仲裁的 DualReadRouter 派送識別碼。
    query_type : QueryType
        驅動規則選擇的查詢類型。
    reason_code : str
        穩定且可供機器搜尋的字串，格式為 "source-level/{query_type}/{outcome}"。
    decided_at_us_utc : int
        判決時的微秒級 UTC 時間戳記，由 ``arbitrate`` 設定，採整數以確保
        JSON 序列化的位元組確定性。
    """

    winning_source: WinningSource
    tie_breaker_rule: str
    conflict_event_id: str | None
    policy_version: str
    correlation_id: str
    query_type: QueryType
    reason_code: str
    decided_at_us_utc: int

    @property
    def requires_manual_review(self) -> bool:
        """判斷此判決是否需要人工審查。

        Returns
        -------
        bool
            當 ``winning_source`` 為 "tie" 或 "fallback" 時回傳 True。
        """
        return self.winning_source in ("tie", "fallback")

    def to_json_line(self) -> str:
        """序列化為單行 JSON，鍵順序固定。

        相同輸入會產生位元組完全相等的輸出。
        ``policy_version`` 恆以非 null 字串輸出。

        Returns
        -------
        str
            具有確定性鍵順序的 JSON 字串。
        """
        payload = {
            "winning_source": self.winning_source,
            "tie_breaker_rule": self.tie_breaker_rule,
            "conflict_event_id": self.conflict_event_id,
            "policy_version": self.policy_version,
            "correlation_id": self.correlation_id,
            "query_type": self.query_type.value,
            "reason_code": self.reason_code,
            "decided_at_us_utc": self.decided_at_us_utc,
        }
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json_line(cls, line: str) -> LiveArbitrationDecision:
        """從 JSON 字串解碼為 LiveArbitrationDecision 實例。

        若序列化資料缺少 ``policy_version`` 鍵（舊版寫入器產出），
        會自動補上 :data:`POLICY_VERSION_PRE_RC` 而非拋出異常。

        Args
        ----
        line : str
            待解碼的 JSON 字串。

        Returns
        -------
        LiveArbitrationDecision
            解碼後的不可變仲裁判決物件。
        """
        data = json.loads(line)
        return cls(
            winning_source=data["winning_source"],
            tie_breaker_rule=data["tie_breaker_rule"],
            conflict_event_id=data.get("conflict_event_id"),
            policy_version=data.get("policy_version", POLICY_VERSION_PRE_RC),
            correlation_id=data["correlation_id"],
            query_type=QueryType(data["query_type"]),
            reason_code=data["reason_code"],
            decided_at_us_utc=int(data["decided_at_us_utc"]),
        )


def _is_empty(evidence: RetrievalEvidence | None) -> bool:
    """判斷 evidence 為 None 或無命中結果時回傳 True。"""
    return evidence is None or len(evidence.hits) == 0


def arbitrate(
    primary: RetrievalEvidence,
    secondary: RetrievalEvidence | None,
    query_type: QueryType,
    correlation_id: str,
) -> LiveArbitrationDecision:
    """套用 Q1 Option A 來源層級規則表進行跨儲存仲裁。

    規則依序判定：
      1. 若 ``secondary`` 為 None 或無命中結果 → ``winning_source="fallback"``。
      2. 若 ``primary`` 無命中結果 → 同樣為 ``"fallback"``。
      3. 否則查閱 ``_QT_OWNERSHIP`` 取得對應的勝出來源；
         未知的 QueryType 解析為 ``"fallback"``。

    此為純函式：無 I/O、無副作用、不修改全域狀態。

    Args
    ----
    primary : RetrievalEvidence
        主要資料來源（Parallax）的檢索結果。
    secondary : RetrievalEvidence or None
        次要資料來源（Aphelion）的檢索結果，可能為 None。
    query_type : QueryType
        驅動規則選擇的查詢類型。
    correlation_id : str
        關聯至本次 DualReadRouter 派送的識別碼。

    Returns
    -------
    LiveArbitrationDecision
        包含仲裁結果的不可變判決物件。
    """
    if _is_empty(secondary) or _is_empty(primary):
        winning_source: WinningSource = "fallback"
    else:
        winning_source = _QT_OWNERSHIP.get(query_type, "fallback")

    reason_code = f"source-level/{query_type.value}/{winning_source}"
    decided_at_us_utc = time.time_ns() // 1_000

    return LiveArbitrationDecision(
        winning_source=winning_source,
        tie_breaker_rule="source-level",
        conflict_event_id=None,
        policy_version=POLICY_VERSION_DEFAULT,
        correlation_id=correlation_id,
        query_type=query_type,
        reason_code=reason_code,
        decided_at_us_utc=decided_at_us_utc,
    )
