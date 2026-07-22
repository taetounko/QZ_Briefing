# -*- coding: utf-8 -*-
"""Pure market-close comparisons, conclusions, and next-session watchlists."""

from __future__ import annotations

from .rules import derivatives_values, index_rates, spot_flows, stock_rates


STATE_SCORE = {
    "strong_bullish": 2, "bullish": 1, "neutral": 0,
    "bearish": -1, "strong_bearish": -2,
}
CONCLUSIONS = {
    "strong_bullish": "상승장", "bullish": "상승장", "neutral": "혼조장",
    "bearish": "약세장", "strong_bearish": "투매 또는 과매도",
    "insufficient_data": "방향성 불명확",
}


def session_snapshot(result: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(result, dict):
        return {}
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    holdings = result.get("holdings_analysis") if isinstance(result.get("holdings_analysis"), dict) else {}
    portfolio = holdings.get("portfolio") if isinstance(holdings.get("portfolio"), dict) else {}
    return {
        "market_state": analysis.get("market_state"),
        "indices": index_rates(result),
        "large_caps": stock_rates(result),
        "spot_flows": spot_flows(result),
        "derivatives": derivatives_values(result),
        "portfolio": {
            "profit_loss": portfolio.get("profit_loss"),
            "profit_rate": portfolio.get("profit_rate"),
        },
    }


def _changes(current: dict[str, object], previous: dict[str, object]) -> dict[str, object]:
    changes: dict[str, object] = {}
    for group in ("indices", "large_caps", "spot_flows", "derivatives", "portfolio"):
        current_group = current.get(group) if isinstance(current.get(group), dict) else {}
        previous_group = previous.get(group) if isinstance(previous.get(group), dict) else {}
        keys = sorted(set(current_group) | set(previous_group))
        changes[group] = {
            key: (
                {"previous": previous_group.get(key), "current": current_group.get(key),
                 "change": current_group[key] - previous_group[key]}
                if isinstance(current_group.get(key), (int, float))
                and isinstance(previous_group.get(key), (int, float))
                else {"previous": previous_group.get(key), "current": current_group.get(key),
                      "change": "not_available"}
            ) for key in keys
        }
    return changes


def compare_market_close(
    current: dict[str, object],
    pre_market: dict[str, object] | None,
    intraday: dict[str, object] | None,
) -> dict[str, object]:
    current_snapshot = session_snapshot(current)
    output: dict[str, object] = {}
    for name, previous in (("pre_market", pre_market), ("intraday_10am", intraday)):
        if not isinstance(previous, dict):
            output[name] = {"available": False, "changes": {}}
            continue
        comparison = _changes(current_snapshot, session_snapshot(previous))
        current_leadership = current.get("leadership") if isinstance(current.get("leadership"), dict) else {}
        prior_leadership = previous.get("leadership") if isinstance(previous.get("leadership"), dict) else {}
        comparison["leadership"] = compare_leadership(current_leadership, prior_leadership)
        comparison["holdings"] = _holding_changes(current, previous)
        previous_analysis = previous.get("analysis") if isinstance(previous.get("analysis"), dict) else {}
        output[name] = {"available": True, "market_state": previous_analysis.get("market_state"), "changes": comparison}
    return output


def _holding_changes(current: dict[str, object], previous: dict[str, object]) -> list[dict[str, object]]:
    def indexed(source: dict[str, object]) -> dict[str, dict[str, object]]:
        holdings = source.get("holdings_analysis") if isinstance(source.get("holdings_analysis"), dict) else {}
        return {str(row.get("code")): row for row in holdings.get("holdings", []) if isinstance(row, dict)}
    current_items, previous_items = indexed(current), indexed(previous)
    output = []
    for code in sorted(current_items.keys() & previous_items.keys()):
        now, before = current_items[code], previous_items[code]
        changes = {}
        for field in ("profit_loss", "profit_rate"):
            old, new = before.get(field), now.get(field)
            changes[field] = new - old if isinstance(old, (int, float)) and isinstance(new, (int, float)) else "not_available"
        output.append({"code": code, "name": now.get("name"), **changes, "trend": {"previous": before.get("trend"), "current": now.get("trend")}, "bottom_confirmation": {"previous": before.get("bottom_confirmation"), "current": now.get("bottom_confirmation")}})
    return output


def evaluate_market_close(
    current: dict[str, object], comparison: dict[str, object]
) -> dict[str, object]:
    analysis = current.get("analysis") if isinstance(current.get("analysis"), dict) else {}
    state = str(analysis.get("market_state") or "insufficient_data")
    pre = comparison.get("pre_market") if isinstance(comparison.get("pre_market"), dict) else {}
    intra = comparison.get("intraday_10am") if isinstance(comparison.get("intraday_10am"), dict) else {}
    pre_state = _previous_state(pre)
    intra_state = _previous_state(intra)
    return {
        "market_conclusion": CONCLUSIONS.get(state, "방향성 불명확"),
        "pre_market_evaluation": _state_evaluation(pre_state, state, premarket=True),
        "intraday_evaluation": _state_evaluation(intra_state, state, premarket=False),
        "flow_summary": _flow_summary(current),
        "leadership_summary": "장마감 신규·유지·이탈 종목을 거래대금과 추세 기준으로 재확인했습니다.",
        "rebound_summary": "반등 후보는 바닥 확인 여부와 추격 위험을 함께 확인해야 합니다.",
        "risk_summary": "수급과 지수 방향이 어긋나거나 데이터가 누락된 항목은 다음 거래일 재확인이 필요합니다.",
        "next_session_summary": "지수 방향, 외국인 현물·선물, 프로그램 수급과 보유종목 저점 방어를 우선 관찰합니다.",
    }


def _previous_state(comparison: dict[str, object]) -> str | None:
    return comparison.get("market_state") if isinstance(comparison.get("market_state"), str) else None


def compare_leadership(current: dict[str, object], previous: dict[str, object]) -> dict[str, list[str]]:
    def codes(source: dict[str, object]) -> set[str]:
        return {str(row.get("code")) for market in ("kospi", "kosdaq") for row in source.get(market, []) if isinstance(row, dict)}
    current_codes, previous_codes = codes(current), codes(previous)
    return {"new": sorted(current_codes - previous_codes), "maintained": sorted(current_codes & previous_codes), "dropped": sorted(previous_codes - current_codes)}


def _state_evaluation(previous: str | None, current: str, *, premarket: bool) -> str:
    if previous not in STATE_SCORE or current not in STATE_SCORE:
        return "판단 자료 부족"
    delta = STATE_SCORE[current] - STATE_SCORE[previous]
    if premarket:
        if delta == 0: return "장전 판단 적중"
        if STATE_SCORE[current] * STATE_SCORE[previous] < 0: return "장중 방향 전환"
        return "일부 적중" if abs(delta) == 1 else "장전 판단 실패"
    if delta == 0: return "횡보"
    if STATE_SCORE[current] * STATE_SCORE[previous] < 0: return "반전"
    return "추세 강화" if abs(STATE_SCORE[current]) > abs(STATE_SCORE[previous]) else "추세 약화"


def _flow_summary(result: dict[str, object]) -> str:
    spot, derivatives = spot_flows(result), derivatives_values(result)
    values = {
        "외국인 현물": spot.get("foreigner"), "기관 현물": spot.get("institution"),
        "프로그램": derivatives.get("program_total"), "외국인 선물": derivatives.get("futures_foreign"),
    }
    known = [f"{name} {'순매수' if value > 0 else '순매도' if value < 0 else '중립'}" for name, value in values.items() if isinstance(value, (int, float))]
    return ", ".join(known) if known else "수급 판단 자료 부족"


def build_next_session_watchlist(result: dict[str, object]) -> list[dict[str, object]]:
    watchlist: list[dict[str, object]] = [{
        "category": "market_indicator", "code": None, "name": "KOSPI·KOSDAQ·KOSPI200",
        "current_state": (result.get("analysis") or {}).get("market_state", "insufficient_data"),
        "confirmation_condition": "지수와 외국인·프로그램 수급 방향 일치",
        "risk_condition": "지수와 수급 방향의 재차 괴리",
    }]
    leadership = result.get("leadership") if isinstance(result.get("leadership"), dict) else {}
    for row in list(leadership.get("kospi", []))[:3] + list(leadership.get("kosdaq", []))[:3]:
        if isinstance(row, dict):
            watchlist.append({"category": "leadership", "code": row.get("code"), "name": row.get("name"), "current_state": row.get("confidence"), "confirmation_condition": "거래대금과 고가 부근 유지", "risk_condition": "거래대금 감소 또는 고가 대비 이탈"})
    holdings = result.get("holdings_analysis") if isinstance(result.get("holdings_analysis"), dict) else {}
    for row in holdings.get("holdings", []):
        if not isinstance(row, dict): continue
        risk = row.get("review_status") in {"exit_review", "reduce_risk", "averaging_down_high_risk"}
        watchlist.append({"category": "holding_risk" if risk else "holding_opportunity", "code": row.get("code"), "name": row.get("name"), "current_state": row.get("trend", "insufficient_data"), "confirmation_condition": "저점 방어와 거래량 동반 추세 개선", "risk_condition": "직전 저점 이탈 또는 추세 약화"})
    return watchlist
