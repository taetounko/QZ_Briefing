# -*- coding: utf-8 -*-
"""Build Korean rule-based interpretation without Qt or external services."""

from __future__ import annotations

from .rules import (
    calculate_signals, derivatives_values, index_rates, score_market, spot_flows,
    stock_rates,
)

STATE_LABELS = {
    "strong_bullish": "강한 상승 우위",
    "bullish": "상승 우위",
    "neutral": "중립·혼조",
    "bearish": "하락 우위",
    "strong_bearish": "강한 하락 우위",
    "insufficient_data": "판단 자료 부족",
}


def analyze_briefing(
    result: dict[str, object], pre_market: dict[str, object] | None = None
) -> dict[str, object]:
    state, score, confidence, reasons = score_market(result)
    signals = calculate_signals(result)
    warnings: list[str] = []
    if state == "insufficient_data":
        warnings.append("시장 상태 판단에 필요한 데이터가 부족합니다.")
    derivatives = derivatives_values(result)
    if derivatives.get("futures_foreign") is None:
        warnings.append("코스피200 선물 최근월물 또는 외국인 선물 수급을 확인할 수 없습니다.")
        if confidence == "high":
            confidence = "medium"
        elif confidence == "medium":
            confidence = "low"
    if derivatives.get("futures_institution") is None:
        warnings.append("기관 선물 수급을 확인할 수 없습니다.")
        if confidence == "high":
            confidence = "medium"
    comparison = compare_with_pre_market(result, pre_market)
    warnings.extend(comparison.pop("warnings", []))
    return {
        "market_state": state,
        "score": score,
        "confidence": confidence,
        "summary": f"종합 점수 {score:+d}점으로 {STATE_LABELS[state]}입니다.",
        "score_reasons": reasons,
        "signals": signals,
        "indicator_comments": build_comments(result),
        "comparison_with_pre_market": comparison,
        "warnings": warnings,
    }


def build_comments(result: dict[str, object]) -> dict[str, str]:
    indices, stocks = index_rates(result), stock_rates(result)
    spot, derivatives = spot_flows(result), derivatives_values(result)
    return {
        "market_indices": comment_group("시장 지수", indices),
        "large_caps": comment_group("대형주", {"삼성전자": stocks.get("005930"), "SK하이닉스": stocks.get("000660")}),
        "spot_flows": flow_comment("현물 수급", spot),
        "program_trading": single_flow_comment("프로그램 전체", derivatives.get("program_total")),
        "derivatives": derivative_comment(derivatives),
    }


def comment_group(title: str, values: dict[str, float | None]) -> str:
    known = {key: value for key, value in values.items() if value is not None}
    if not known:
        return f"{title} 데이터가 없습니다.\n현재 해석: 방향을 판단할 수 없습니다.\n주의: 데이터 확인 전 추정하지 않습니다."
    detail = ", ".join(f"{key} {value:+.2f}%" for key, value in known.items())
    positive = sum(value > 0 for value in known.values())
    tone = "상승 우위" if positive > len(known) / 2 else "하락 우위" if positive < len(known) / 2 else "혼조"
    return f"{title}는 {detail}입니다.\n지표를 함께 보면 {tone} 흐름입니다.\n현재 해석: {tone}입니다.\n주의: 단일 시점 등락률은 장중 변동으로 바뀔 수 있습니다."


def flow_comment(title: str, values: dict[str, int | None]) -> str:
    names = {"individual": "개인", "foreigner": "외국인", "institution": "기관계"}
    known = [(names.get(key, key), value) for key, value in values.items() if value is not None]
    if not known:
        return f"{title} 데이터가 없습니다.\n현재 해석: 수급 방향을 판단할 수 없습니다.\n주의: 공식 수집값이 없는 항목은 추정하지 않습니다."
    detail = ", ".join(f"{name} {value:+,}" for name, value in known)
    return f"{title}은 {detail}입니다.\n주요 주체의 순매수 방향을 지수와 함께 확인해야 합니다.\n현재 해석: 순매수 부호 기준으로 수급 방향을 구분했습니다.\n주의: 단위는 collector의 공식 단위 정보를 확인해야 합니다."


def single_flow_comment(title: str, value: int | None) -> str:
    if value is None:
        return f"{title} 데이터가 없습니다.\n현재 해석: 판단 불가입니다.\n주의: 누락값을 0으로 간주하지 않습니다."
    direction = "순매수" if value > 0 else "순매도" if value < 0 else "보합"
    return f"{title} 순매수는 {value:+,}백만원입니다.\n외국인 현물 수급과 같은 방향인지 확인합니다.\n현재 해석: {direction} 우위입니다.\n주의: 장중 집계값은 변할 수 있습니다."


def derivative_comment(values: dict[str, int | None]) -> str:
    foreign = values.get("futures_foreign")
    oi = values.get("open_interest")
    if foreign is None and oi is None:
        return "선물 수급과 OI 데이터가 없습니다.\n현재 해석: 파생시장 확인이 불가능합니다.\n주의: 최근월물을 추정하지 않습니다."
    return f"외국인 선물 순매수는 {format_optional(foreign)}, 미결제약정은 {format_optional(oi)}입니다.\n현물 수급과 선물 방향을 함께 확인합니다.\n현재 해석: 확인 가능한 공식 값만 반영했습니다.\n주의: OI 현재값만으로 증감 방향을 추정하지 않습니다."


def compare_with_pre_market(
    current: dict[str, object], previous: dict[str, object] | None
) -> dict[str, object]:
    if not previous:
        return {"available": False, "warnings": ["장전 브리핑 파일이 없어 비교를 건너뜁니다."]}
    changes: dict[str, object] = {}
    for label, current_value, previous_value in (
        ("KOSPI", index_rates(current).get("KOSPI"), index_rates(previous).get("KOSPI")),
        ("삼성전자", stock_rates(current).get("005930"), stock_rates(previous).get("005930")),
        ("SK하이닉스", stock_rates(current).get("000660"), stock_rates(previous).get("000660")),
    ):
        if current_value is not None and previous_value is not None:
            changes[label] = {"pre_market": previous_value, "current": current_value, "change": current_value - previous_value}
    previous_analysis = previous.get("analysis")
    if isinstance(previous_analysis, dict):
        changes["market_state"] = {"pre_market": previous_analysis.get("market_state"), "current": score_market(current)[0]}
    old_signals = set(previous_analysis.get("signals", [])) if isinstance(previous_analysis, dict) else set()
    changes["new_signals"] = [signal for signal in calculate_signals(current) if signal not in old_signals]
    warnings = [] if changes else ["장전 브리핑의 비교 가능한 필드가 부족합니다."]
    return {"available": True, "changes": changes, "warnings": warnings}


def format_optional(value: int | None) -> str:
    return f"{value:+,}" if value is not None else "데이터 없음"
