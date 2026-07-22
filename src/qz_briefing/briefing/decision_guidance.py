# -*- coding: utf-8 -*-
"""Explainable market and position guidance; no collection or order operations."""

from __future__ import annotations

from .rules import derivatives_values, index_rates, spot_flows, stock_rates

MARKET_LABELS = {
    "strong_uptrend": "강한 상승 추세", "gradual_uptrend": "완만한 상승",
    "technical_rebound": "기술적 반등", "upward_attempt": "상승 시도",
    "mixed": "혼조", "directionless": "방향성 없음",
    "downturn": "하락 전환", "weak_decline": "약한 하락",
    "strong_decline": "강한 하락", "capitulation": "투매성 하락",
    "bottom_search": "바닥 탐색", "rebound_failure": "반등 실패",
    "insufficient_data": "데이터 부족", "market_not_open": "시장 미개장",
}

ACTION_LABELS = {
    "do_not_add": "추가매수 금지", "observe_only": "관찰만",
    "conditional_add_review": "조건 충족 시 추가매수 검토",
    "hold_and_monitor": "보유하며 관찰", "protect_profit": "수익 보호",
    "reduce_risk": "위험 축소 검토", "exit_condition_check": "이탈 조건 확인",
    "insufficient_data": "자료 부족",
}


def _known(values):
    return [float(value) for value in values if isinstance(value, (int, float))]


def market_decision(result: dict[str, object]) -> dict[str, object]:
    if result.get("status") == "no_market_open":
        return _market("market_not_open", 95, ["공식 장 개시 또는 실제 체결 신호가 확인되지 않음"], [], "시장 개시 여부만 다시 확인합니다.", "low")
    indices, stocks = index_rates(result), stock_rates(result)
    spot, derivative = spot_flows(result), derivatives_values(result)
    rates = _known([*indices.values(), *stocks.values()])
    flow_values = _known([spot.get("foreigner"), spot.get("institution"), derivative.get("program_total"), derivative.get("futures_foreign")])
    warnings = list(result.get("warnings", [])) + list(result.get("errors", []))
    if len(rates) < 2:
        return _market("insufficient_data", max(10, 35 - len(warnings) * 5), ["확인 가능한 지수·대형주 값이 부족함"], [], "값을 추정하지 않고 다음 브리핑에서 다시 확인합니다.", "high")
    positive = sum(value > 0 for value in rates); negative = sum(value < 0 for value in rates)
    average = sum(rates) / len(rates)
    evidence = [f"확인 지표 평균 등락률 {average:+.2f}%", f"상승 {positive}개·하락 {negative}개"]
    conflicts = []
    foreign_spot, foreign_futures = spot.get("foreigner"), derivative.get("futures_foreign")
    if isinstance(foreign_spot, (int, float)) and isinstance(foreign_futures, (int, float)) and foreign_spot * foreign_futures < 0:
        conflicts.append("외국인 현물과 선물 수급 방향이 서로 다름")
    if average > 0 and flow_values and sum(flow_values) < 0:
        conflicts.append("지수는 상승하지만 확인 가능한 수급 합계는 약세")
    if average < 0 and flow_values and sum(flow_values) > 0:
        conflicts.append("수급은 개선되지만 지수 방향은 약세")
    agreement = max(positive, negative) / len(rates)
    completeness = min(1.0, (len(rates) + len(flow_values)) / 9)
    confidence = round(max(15, min(95, 35 + agreement * 35 + completeness * 25 - len(conflicts) * 12 - len(warnings) * 3)))
    if average <= -3 and negative == len(rates): state = "capitulation"
    elif average <= -1.5: state = "strong_decline"
    elif average < -0.3: state = "weak_decline"
    elif average >= 1.5 and positive == len(rates): state = "strong_uptrend"
    elif average >= 0.5: state = "gradual_uptrend"
    elif average > 0 and negative: state = "mixed"
    elif average < 0 and positive: state = "technical_rebound" if rates[-1] > 0 else "mixed"
    else: state = "directionless"
    risk = "high" if state in {"capitulation", "strong_decline", "rebound_failure"} else "medium" if conflicts or confidence < 60 else "low"
    guidance = "확인 조건이 충족될 때까지 관찰을 우선합니다." if confidence < 60 else "현재 방향을 추격하지 말고 유지 조건을 확인합니다."
    return _market(state, confidence, evidence, conflicts, guidance, risk)


def _market(state, confidence, evidence, conflicts, guidance, risk):
    cautious = "로 판단됩니다" if confidence >= 60 else "가능성이 있으나 단정하기 어렵습니다"
    label = MARKET_LABELS[state]
    return {
        "state": state, "confidence": confidence,
        "headline": f"현재 시장은 {label} 구간{cautious}.", "evidence": evidence,
        "conflicts": conflicts,
        "confirmation_conditions": ["지수와 대형주 방향 일치", "거래대금과 현물·선물 수급 동반"],
        "invalidation_conditions": ["주요 지수 방향 반전", "외국인 선물·프로그램 수급 이탈"],
        "action_guidance": guidance, "risk_level": risk,
    }


def holding_decision(item: dict[str, object], market: dict[str, object] | None = None) -> dict[str, object]:
    trend, bottom, review = item.get("trend"), item.get("bottom_confirmation"), item.get("review_status")
    current = item.get("current_price"); profit = item.get("profit_rate")
    mas = item.get("moving_averages") if isinstance(item.get("moving_averages"), dict) else {}
    levels = item.get("high_low") if isinstance(item.get("high_low"), dict) else {}
    if trend == "insufficient_data" or not isinstance(current, (int, float)):
        return _holding(review or "insufficient_data", 20, "기술 데이터가 부족해 손실률만으로 판단하지 않습니다.", ["추세·저점 데이터 부족"], [], ["일봉 데이터 확보"], {}, "자료 확보 후", "insufficient_data", "unknown")
    reasons = [f"추세 상태 {trend}", f"바닥 확인 상태 {bottom}"]
    if isinstance(item.get("rsi14"), (int, float)):
        reasons.append(f"RSI14 {float(item['rsi14']):.1f}")
    macd = item.get("macd") if isinstance(item.get("macd"), dict) else {}
    if isinstance(macd.get("histogram"), (int, float)):
        reasons.append(f"MACD 히스토그램 {float(macd['histogram']):+.2f}")
    if isinstance(item.get("volume_multiple"), (int, float)):
        reasons.append(f"20일 평균 대비 거래량 {float(item['volume_multiple']):.2f}배")
    positive, risks, conflicts = [], [], []
    action, risk = "hold_and_monitor", "medium"
    if review == "exit_review" or bottom == "failed":
        action, risk = "exit_condition_check", "very_high"; risks.append("최근 저점 또는 설정 손절 조건 이탈")
        summary = "핵심 저점 이탈로 기존 보유 근거가 약해져 이탈 조건 확인이 우선입니다."
    elif review == "averaging_down_high_risk" or trend == "strong_downtrend":
        action, risk = "do_not_add", "very_high"
        risks.append("강한 하락추세에서 저점 확인 없이 추가매수하면 손실이 확대될 수 있음")
        summary = "손실률이 크다는 이유만으로 추가매수하면 안 됩니다. 추세가 멈췄다는 근거가 확인되지 않았습니다."
    elif review in {"reduce_risk", "reduce_position_review"}:
        action, risk = "reduce_risk", "high"; risks.append("반등 지속 또는 장기 추세 회복 근거 부족")
        summary = "반등을 추격하기보다 저항 돌파 실패 시 위험 축소를 검토할 구간입니다."
    elif review == "averaging_down_candidate":
        action, risk = "conditional_add_review", "medium"; positive.extend(["저점 재이탈 없음", "20일선 지지와 거래량 증가"])
        summary = "바닥 개선이 관찰되지만 후보는 매수 신호가 아닙니다. 확인 조건 충족 시에만 추가매수를 검토합니다."
    elif review in {"add_on_strength_candidate", "adding_to_winner_candidate"}:
        action, risk = "protect_profit", "medium"; positive.extend(["20일선 위 유지", "최근 고점 돌파와 거래량 동반"])
        summary = "수익권 상승추세지만 추격 위험을 관리하며 돌파 유지 여부를 확인합니다."
    else:
        summary = "현재 추세를 보유하며 관찰하되 확인 조건이 깨지면 판단을 다시 점검합니다."
    market_state = market.get("state") if isinstance(market, dict) else None
    if trend in {"uptrend", "strong_uptrend"} and market_state in {"strong_decline", "capitulation"}:
        conflicts.append("종목 단기 추세는 상승이나 전체 시장은 강한 약세")
    confidence = 75 - len(conflicts) * 15 - len(item.get("warnings", [])) * 5
    if bottom in {"confirmed", "partially_confirmed"}: confidence += 8
    prices = _price_conditions(float(current), mas, levels, action)
    return _holding(review, max(20, min(95, confidence)), summary, reasons, conflicts, positive, prices, item.get("next_session_observation") or "다음 브리핑", action, risk, risks)


def _price_conditions(current, mas, levels, action):
    output = {}
    for key, value in (("ma5", mas.get("ma5")), ("ma20", mas.get("ma20")), ("ma60", mas.get("ma60")), ("recent_low", levels.get("low20")), ("recent_high", levels.get("high20"))):
        if isinstance(value, (int, float)) and value > 0: output[key] = round(float(value), 2)
    if action == "conditional_add_review" and isinstance(mas.get("ma20"), (int, float)) and mas["ma20"] <= current:
        output["additional_review_price"] = round(float(mas["ma20"]), 2)
    if isinstance(levels.get("low20"), (int, float)) and levels["low20"] <= current:
        output["invalidation_price"] = round(float(levels["low20"]), 2)
    if isinstance(levels.get("high20"), (int, float)) and levels["high20"] >= current:
        output["major_resistance"] = round(float(levels["high20"]), 2)
        if action == "reduce_risk":
            output["reduction_review_price"] = round(float(levels["high20"]), 2)
    if action == "protect_profit" and isinstance(mas.get("ma20"), (int, float)) and mas["ma20"] < current:
        output["profit_protection_price"] = round(float(mas["ma20"]), 2)
    return output


def _holding(review, confidence, summary, reasons, conflicts, positive, prices, next_check, action, risk, risks=None):
    return {"review_status": review, "confidence": confidence, "summary": summary,
            "reasons": reasons, "conflicts": conflicts, "positive_conditions": positive,
            "risk_conditions": risks or [], "price_conditions": prices,
            "next_check": next_check, "action_level": action, "position_risk": risk}


def priority(decision: dict[str, object]) -> int:
    action = decision.get("action_level")
    return {"exit_condition_check": 1, "reduce_risk": 2, "do_not_add": 3,
            "observe_only": 4, "conditional_add_review": 5, "protect_profit": 6,
            "hold_and_monitor": 7, "insufficient_data": 8}.get(str(action), 8)
