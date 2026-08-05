# -*- coding: utf-8 -*-
"""Pure pre-market data-quality checks; no collection or external I/O."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from .rules import collector_data, derivatives_values, index_rates, spot_flows, stock_rates


CORE_COLLECTORS = (
    "kiwoom_core_market",
    "kiwoom_market_indices",
    "kiwoom_investor_flows",
    "kiwoom_derivatives_flows",
    "kiwoom_market_leadership",
)
MAX_COLLECTION_SPAN_SECONDS = 45


def _parsed_at(data: dict[str, Any]) -> datetime | None:
    value = data.get("collected_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _phase(moment: datetime) -> str:
    return "preopen" if moment.time() < time(9, 0) else "opening_live"


def _zero_opening_snapshot(result: dict[str, object]) -> bool:
    indices = collector_data(result, "kiwoom_market_indices").get("indices", [])
    securities = collector_data(result, "kiwoom_core_market").get("securities", [])
    rows = [row for row in (*indices, *securities) if isinstance(row, dict)] if isinstance(indices, list) and isinstance(securities, list) else []
    if not rows:
        return False
    fields = ("open", "high", "low", "volume", "change_rate")
    observed = [row.get(field) for row in rows for field in fields if field in row]
    return len(observed) >= 5 and all(value in (0, 0.0, None, "") for value in observed)


def evaluate_pre_market_quality(result: dict[str, object]) -> dict[str, object]:
    """Classify a pre-market payload without treating zero snapshots as flat markets."""
    data = {name: collector_data(result, name) for name in CORE_COLLECTORS}
    moments = {name: _parsed_at(value) for name, value in data.items()}
    known = [value for value in moments.values() if value is not None]
    phases = {_phase(value) for value in known}
    span = (max(known) - min(known)).total_seconds() if len(known) > 1 else 0
    consistent = len(phases) <= 1 and span <= MAX_COLLECTION_SPAN_SECONDS
    zero_snapshot = _zero_opening_snapshot(result)

    indices = index_rates(result)
    stocks = stock_rates(result)
    derivatives = derivatives_values(result)
    spot = spot_flows(result)
    index_valid = all(indices.get(key) is not None for key in ("KOSPI", "KOSDAQ"))
    caps_valid = all(stocks.get(code) is not None for code in ("005930", "000660"))
    program_valid = derivatives.get("program_total") is not None
    spot_explicitly_unavailable = data["kiwoom_investor_flows"].get("data_status") in {"unavailable", "pending"}
    spot_valid = any(value is not None for value in spot.values()) or spot_explicitly_unavailable
    all_zero_flow = bool(spot) and all(value == 0 for value in spot.values())
    flow_unit = data["kiwoom_investor_flows"].get("unit")
    flow_usable = spot_valid and not (all_zero_flow and not flow_unit)

    reasons: list[str] = []
    if zero_snapshot: reasons.append("09:00 직후 OHLC·거래량·등락률이 모두 0인 미완성 스냅샷")
    if not consistent: reasons.append("핵심 데이터 수집 시각 또는 시장 구간 불일치")
    if not index_valid: reasons.append("시장 지수 자료 부족")
    if not caps_valid: reasons.append("삼성전자·SK하이닉스 자료 부족")
    if not program_valid: reasons.append("프로그램 수급 자료 부족")
    if not flow_usable: reasons.append("현물 수급 자료 또는 공식 단위 불명확")

    if zero_snapshot or not consistent or not index_valid or not caps_valid:
        quality, status = "unavailable", "incomplete"
    elif program_valid and flow_usable:
        quality, status = "complete", "completed"
    else:
        quality, status = "partial", "degraded"
    return {
        "DATA_QUALITY": "unavailable" if quality == "unavailable" else quality,
        "BRIEFING_QUALITY": quality,
        "status": status,
        "collection_phase": next(iter(phases)) if len(phases) == 1 else "mixed_or_unknown",
        "collection_span_seconds": span,
        "time_consistent": consistent,
        "market_score_allowed": quality != "unavailable" and flow_usable,
        "spot_flow_usable": flow_usable,
        "reasons": reasons,
        "collected_at": {name: value.isoformat() if value else None for name, value in moments.items()},
    }


def unavailable_analysis(quality: dict[str, object]) -> dict[str, object]:
    warning = "핵심 시장 데이터 수집이 완료되지 않아 판단을 보류합니다."
    return {
        "market_state": "insufficient_data", "score": None, "confidence": "low",
        "summary": warning, "score_reasons": [], "signals": [], "indicator_comments": {},
        "comparison_with_pre_market": {"available": False},
        "warnings": [warning, *[str(value) for value in quality.get("reasons", [])]],
        "decision": {"headline": warning, "confidence": "low", "action_guidance": "추가 데이터 확인이 필요합니다."},
    }
