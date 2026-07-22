# -*- coding: utf-8 -*-
"""Render the persisted rule-based analysis as a Korean briefing document."""

from __future__ import annotations

from .rules import derivatives_values, index_rates, spot_flows, stock_rates


def render_markdown(result: dict[str, object]) -> str:
    analysis = result.get("analysis", {})
    comments = analysis.get("indicator_comments", {}) if isinstance(analysis, dict) else {}
    decision = analysis.get("decision", {}) if isinstance(analysis, dict) and isinstance(analysis.get("decision"), dict) else {}
    holdings = result.get("holdings_analysis") if isinstance(result.get("holdings_analysis"), dict) else {}
    lines = [
        "# QZ 한국 시장 브리핑", "",
        f"- 거래일: {result['trading_date']}",
        f"- 브리핑 종류: {result['briefing_type']}",
        f"- Briefing type: {result['briefing_type']}",
        f"- 생성 상태: {result['status']}", "",
        "## 오늘의 결론", "",
        f"- 시장 상태: {decision.get('headline', analysis.get('summary', '분석 데이터가 없습니다.'))}",
        f"- 판단 신뢰도: {decision.get('confidence', 0)}/100",
        f"- 시장 위험 수준: {decision.get('risk_level', 'unknown')}",
        f"- 행동지침: {decision.get('action_guidance', '관찰을 우선합니다.')}",
        f"- 핵심 확인 조건: {', '.join(decision.get('confirmation_conditions', [])) or '자료 확보'}",
        f"- 핵심 위험 조건: {', '.join(decision.get('invalidation_conditions', [])) or '자료 부족'}", "",
        "## 보유종목 긴급 확인", "",
    ]
    urgent = sorted(
        [item for item in holdings.get("holdings", []) if isinstance(item, dict)],
        key=lambda item: item.get("priority", 8),
    )[:5]
    if urgent:
        for item in urgent:
            item_decision = item.get("decision", {}) if isinstance(item.get("decision"), dict) else {}
            lines.append(
                f"- {item.get('name')}({item.get('code')}): {item_decision.get('summary', '자료 부족')} "
                f"/ 다음 확인: {item_decision.get('next_check', '자료 확보')} "
                f"/ 행동: {item_decision.get('action_level', 'insufficient_data')}"
            )
    else:
        lines.append("- 긴급 확인 대상이 없습니다.")
    lines.extend(["", "## 오늘의 관찰 종목", ""])
    leadership = result.get("leadership") if isinstance(result.get("leadership"), dict) else {}
    observations = []
    for key in ("kospi", "kosdaq", "rebound_candidates"):
        for item in leadership.get(key, []):
            if isinstance(item, dict) and item.get("code") not in {row.get("code") for row in observations}:
                observations.append(item)
    for item in observations[:5]:
        lines.append(
            f"- {item.get('name')}({item.get('code')}): {', '.join(item.get('reasons', [])) or '선정 자료 부족'} "
            f"/ 확인: 거래대금과 추세 유지 / 무효: {', '.join(item.get('warnings', [])) or '선정 기준 이탈'}"
        )
    if not observations:
        lines.append("- 선정 기준을 통과한 관찰 종목이 없습니다.")
    lines.extend([
        "- 선정 결과는 매수 신호가 아닙니다.", "",
        "## 한눈에 보는 시장 판단", "",
        str(analysis.get("summary", "분석 데이터가 없습니다.")),
        f"- 상태: `{analysis.get('market_state', 'insufficient_data')}`",
        f"- 신뢰도: `{analysis.get('confidence', 'low')}`", "",
        "## 핵심 수치", "",
    ])
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
    if result.get("briefing_type") == "market_close":
        lines.extend(render_market_close(result))
    elif result.get("briefing_type") == "pre_market":
        lines.extend(render_previous_market_close(result.get("previous_market_close")))
    return "\n".join(lines) + "\n"


def render_market_close(result: dict[str, object]) -> list[str]:
    analysis = result.get("market_close_analysis") if isinstance(result.get("market_close_analysis"), dict) else {}
    comparison = result.get("session_comparison") if isinstance(result.get("session_comparison"), dict) else {}
    lines = [
        "", "# 장마감 브리핑", "",
        f"## 오늘 시장 한 줄 결론", "", str(analysis.get("market_conclusion", "방향성 불명확")),
        "", "## 장전 예상 대비 결과", "", str(analysis.get("pre_market_evaluation", "판단 자료 부족")),
        "", "## 오전 10시 대비 결과", "", str(analysis.get("intraday_evaluation", "판단 자료 부족")),
        "", "## 지수·대형주 결산", "", "- 장전·10시·장마감 수치 중 확인 가능한 값만 비교했습니다.",
        "", "## 외국인·기관·프로그램·파생 수급", "", str(analysis.get("flow_summary", "수급 판단 자료 부족")),
        "", "## 시장 주도주 결산", "", str(analysis.get("leadership_summary", "자료 부족")),
        "", "## 반등 후보 결산", "", str(analysis.get("rebound_summary", "자료 부족")),
        "", "## 다음 거래일 핵심 관찰 목록", "",
    ]
    watchlist = result.get("next_session_watchlist", [])
    if isinstance(watchlist, list) and watchlist:
        for item in watchlist:
            if isinstance(item, dict):
                lines.append(f"- [{item.get('category')}] {item.get('name') or item.get('code')}: {item.get('current_state')} / 확인: {item.get('confirmation_condition')} / 위험: {item.get('risk_condition')}")
    else:
        lines.append("- 관찰 목록 데이터가 없습니다.")
    lines.extend(["", "## 장마감 위험요인", "", str(analysis.get("risk_summary", "별도 위험요인 없음"))])
    if not any(isinstance(value, dict) and value.get("available") for value in comparison.values()):
        lines.extend(["", "- 장전·10시 비교자료가 없어 장마감 단독 분석입니다."])
    return lines


def render_previous_market_close(value: object) -> list[str]:
    lines = ["", "## 전 거래일 장마감 요약", ""]
    if not isinstance(value, dict):
        lines.append("- 유효한 이전 장마감 결과가 없습니다.")
        return lines
    analysis = value.get("market_close_analysis") if isinstance(value.get("market_close_analysis"), dict) else {}
    lines.extend([
        f"- 기준 거래일: {value.get('trading_date')}",
        f"- 시장 결론: {analysis.get('market_conclusion', '방향성 불명확')}",
        f"- 수급: {analysis.get('flow_summary', '자료 부족')}",
        f"- 다음 거래일: {analysis.get('next_session_summary', '관찰 자료 부족')}",
    ])
    return lines


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
        decision = item.get("decision", {}) if isinstance(item.get("decision"), dict) else {}
        lines.extend([
            "", f"### {item.get('name') or '종목명 없음'} ({item.get('code')})", "",
            f"- 수량 {item.get('quantity'):,}주 / 평단 {item.get('average_price'):,.2f}원 / 현재가 {item.get('current_price'):,}원",
            f"- 평가손익 {item.get('profit_loss'):+,.0f}원 / 수익률 {item.get('profit_rate'):+.2f}%",
            f"- 추세 상태: `{item.get('trend')}`",
            f"- 기술적 위치: 5일선 {optional_number(item.get('moving_averages', {}).get('ma5'))}, 20일선 {optional_number(item.get('moving_averages', {}).get('ma20'))}, 60일선 {optional_number(item.get('moving_averages', {}).get('ma60'))}",
            f"- 바닥 확인 상태: `{item.get('bottom_confirmation')}`",
            f"- 물타기·불타기·축소 검토: `{item.get('review_status')}`",
            f"- 판단 요약: {decision.get('summary', '자료 부족')}",
            f"- 판단 신뢰도: {decision.get('confidence', 0)}/100 / 행동 수준: `{decision.get('action_level', 'insufficient_data')}`",
            f"- 확인 조건: {', '.join(decision.get('positive_conditions', [])) or '추가 자료 확인'}",
            f"- 위험 조건: {', '.join(decision.get('risk_conditions', [])) or '명시된 위험 조건 없음'}",
            f"- 가격 조건: {decision.get('price_conditions', {})}",
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
