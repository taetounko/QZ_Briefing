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
    lines.extend(render_holdings(result.get("holdings_analysis")))
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


def render_holdings(value: object) -> list[str]:
    data = value if isinstance(value, dict) else {}
    portfolio = data.get("portfolio", {}) if isinstance(data.get("portfolio"), dict) else {}
    lines = ["", "## 보유종목 종합", ""]
    if data.get("source") == "kiwoom_accounts":
        lines.append("- 조회 방식: 로그인 계좌 자동조회 (계좌번호는 마스킹하여 저장)")
        for account in data.get("accounts", []):
            if not isinstance(account, dict):
                continue
            summary = account.get("summary", {}) if isinstance(account.get("summary"), dict) else {}
            if account.get("status") == "completed":
                lines.append(
                    f"- 계좌 {account.get('account_id')}: {account.get('holding_count', 0)}종목, "
                    f"평가금액 {quantity(summary.get('market_value'), '원')}, "
                    f"평가손익 {quantity(summary.get('unrealized_profit'), '원')}"
                )
            else:
                lines.append(f"- 계좌 {account.get('account_id')}: 조회 실패 (다른 계좌 분석은 계속)")
    elif data.get("source") == "manual_fallback":
        lines.append("- 조회 방식: 자동 계좌조회 실패로 수동 설정 사용")
    if not data.get("holdings"):
        lines.append("- 등록된 보유종목이 없거나 분석할 수 없습니다.")
    else:
        lines.extend([
            f"- 기준: {'최근 종가' if data.get('basis') == 'latest_close' else '당일 현재가'}",
            f"- 총 투자금: {quantity(portfolio.get('investment_amount'), '원')}",
            f"- 평가금액: {quantity(portfolio.get('valuation_amount'), '원')}",
            f"- 평가손익: {quantity(portfolio.get('profit_loss'), '원')}",
            f"- 전체 수익률: {percent(portfolio.get('profit_rate'))}",
        ])
    for item in data.get("holdings", []):
        lines.extend([
            "", f"### {item.get('name') or '종목명 없음'} ({item.get('code')})", "",
            f"- 수량 {item.get('quantity'):,}주 / 평단 {item.get('average_price'):,.2f}원 / 현재가 {item.get('current_price'):,}원",
            f"- 평가손익 {item.get('profit_loss'):+,.0f}원 / 수익률 {item.get('profit_rate'):+.2f}%",
            f"- 추세 상태: `{item.get('trend')}`",
            f"- 기술적 위치: 5일선 {optional_number(item.get('moving_averages', {}).get('ma5'))}, 20일선 {optional_number(item.get('moving_averages', {}).get('ma20'))}, 60일선 {optional_number(item.get('moving_averages', {}).get('ma60'))}",
            f"- 바닥 확인 상태: `{item.get('bottom_confirmation')}`",
            f"- 물타기·불타기·축소 검토: `{item.get('review_status')}`",
            "- 주의: 수수료와 세금은 반영하지 않았으며 확정적인 매수·매도 지시가 아닙니다.",
        ])
    comparison = data.get("comparison_with_pre_market")
    if comparison:
        lines.extend(["", "### 장전 대비 보유종목 상태 변화", ""])
        for change in comparison:
            lines.append(f"- {change['code']}: 추세 {change['trend']['pre_market']} → {change['trend']['current']}, 검토 {change['review_status']['pre_market']} → {change['review_status']['current']}")
    return lines


def optional_number(value: object) -> str:
    return f"{value:,.2f}" if isinstance(value, (int, float)) else "데이터 없음"


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
