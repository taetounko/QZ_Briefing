# -*- coding: utf-8 -*-
"""Render the persisted rule-based analysis as a Korean briefing document."""

from __future__ import annotations

from .rules import derivatives_values, index_rates, spot_flows, stock_rates


def render_markdown(result: dict[str, object]) -> str:
    analysis = result.get("analysis", {})
    comments = analysis.get("indicator_comments", {}) if isinstance(analysis, dict) else {}
    lines = [
        "# QZ 한국 시장 브리핑", "",
        f"- 거래일: {result['trading_date']}",
        f"- 브리핑 종류: {result['briefing_type']}",
        f"- Briefing type: {result['briefing_type']}",
        f"- 생성 상태: {result['status']}", "",
        "## 한눈에 보는 시장 판단", "",
        str(analysis.get("summary", "분석 데이터가 없습니다.")),
        f"- 상태: `{analysis.get('market_state', 'insufficient_data')}`",
        f"- 신뢰도: `{analysis.get('confidence', 'low')}`", "",
        "## 핵심 수치", "",
    ]
    lines.extend(core_number_lines(result))
    sections = (
        ("시장 지수 해석", "market_indices"),
        ("삼성전자·SK하이닉스 해석", "large_caps"),
        ("현물 투자자 수급", "spot_flows"),
        ("프로그램 매매", "program_trading"),
        ("선물 수급 및 OI", "derivatives"),
    )
    for title, key in sections:
        lines.extend(["", f"## {title}", "", str(comments.get(key, "데이터가 없습니다."))])
    lines.extend(["", "## 장전 대비 변화", ""])
    comparison = analysis.get("comparison_with_pre_market", {})
    if isinstance(comparison, dict) and comparison.get("available"):
        lines.extend(comparison_lines(comparison))
    else:
        lines.append("- 비교 가능한 장전 브리핑이 없습니다.")
    lines.extend(["", "## 핵심 신호", ""])
    signals = analysis.get("signals", [])
    lines.extend(f"- {signal}" for signal in signals) if signals else lines.append("- 확인된 신호 없음")
    lines.extend(["", "## 주의사항", ""])
    warnings = list(result.get("warnings", [])) + list(analysis.get("warnings", []))
    lines.extend(f"- {warning}" for warning in warnings) if warnings else lines.append("- 별도 주의사항 없음")
    lines.extend(["", "## 수집 오류 및 누락 데이터", ""])
    errors = result.get("errors", [])
    lines.extend(f"- {error}" for error in errors) if errors else lines.append("- 수집 오류 없음")
    lines.extend(render_leadership(result.get("leadership")))
    return "\n".join(lines) + "\n"


def render_leadership(value: object) -> list[str]:
    lines: list[str] = []
    leadership = value if isinstance(value, dict) else {}
    for key, title in (("kospi", "코스피 주도주 TOP 10"), ("kosdaq", "코스닥 주도주 TOP 10"), ("rebound_candidates", "바닥 확인 후 반등 후보")):
        lines.extend(["", f"## {title}", ""])
        rows = leadership.get(key, [])
        if not rows:
            lines.append("- 선정 기준을 통과한 종목이 없습니다.")
            continue
        for index, row in enumerate(rows, 1):
            lines.append(f"{index}. {row.get('name') or '종목명 없음'}({row.get('code')}) — 점수 {row.get('score')}")
            lines.append(f"   현재가 {quantity(row.get('current_price'), '원')} / 등락률 {percent(row.get('change_rate'))} / 거래대금 {quantity(row.get('trading_value'), '공식 단위')}")
            lines.append(f"   선정 이유: {', '.join(row.get('reasons', [])) or '데이터 부족'}")
            lines.append(f"   주의: {', '.join(row.get('warnings', [])) or '점수는 매수 지시가 아닙니다.'}")
    lines.extend(["", "## 장전 대비 신규·유지·탈락", ""])
    comparison = leadership.get("comparison_with_pre_market")
    if isinstance(comparison, dict):
        for key, label in (("new", "신규"), ("maintained", "유지"), ("dropped", "탈락")):
            lines.append(f"- {label}: {', '.join(comparison.get(key, [])) or '없음'}")
    else:
        lines.append("- 비교 가능한 장전 후보가 없습니다.")
    lines.extend(["", "## 선정 기준과 주의사항", "", "- 상승률뿐 아니라 거래대금 순위, 시가·고가 위치, 상대강도와 기술 이력을 함께 평가합니다.", "- 선정 결과는 시장 관찰용이며 매수 지시나 수익 보장이 아닙니다."])
    return lines


def core_number_lines(result: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for name, value in index_rates(result).items():
        lines.append(f"- {name} 등락률: {percent(value)}")
    labels = {"005930": "삼성전자", "000660": "SK하이닉스"}
    for code, value in stock_rates(result).items():
        lines.append(f"- {labels.get(code, code)} 등락률: {percent(value)}")
    for investor, value in spot_flows(result).items():
        lines.append(f"- 코스피 {investor} 순매수: {quantity(value)}")
    derivatives = derivatives_values(result)
    lines.append(f"- 프로그램 전체 순매수: {quantity(derivatives.get('program_total'), '백만원')}")
    lines.append(f"- 외국인 선물 순매수: {quantity(derivatives.get('futures_foreign'), '계약')}")
    lines.append(f"- 미결제약정 OI: {quantity(derivatives.get('open_interest'), '계약')}")
    return lines or ["- 핵심 수치 데이터 없음"]


def comparison_lines(comparison: dict[str, object]) -> list[str]:
    changes = comparison.get("changes")
    if not isinstance(changes, dict):
        return ["- 비교 가능한 항목이 없습니다."]
    lines = []
    for name, values in changes.items():
        if name == "new_signals":
            lines.append(f"- 새 신호: {', '.join(values) if values else '없음'}")
        elif isinstance(values, dict):
            lines.append(f"- {name}: {values.get('pre_market')} → {values.get('current')}")
    return lines


def percent(value: float | None) -> str:
    return f"{value:+,.2f}%" if value is not None else "데이터 없음"


def quantity(value: int | None, unit: str = "official scale unspecified") -> str:
    return f"{value:+,} {unit}" if value is not None else "데이터 없음"
