# -*- coding: utf-8 -*-
from __future__ import annotations
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

ACCOUNT = re.compile(r"(?<!\d)\d{8,12}(?!\d)")

def mask_sensitive(text: str) -> str:
    return ACCOUNT.sub(lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:], text)

def escape_markdown(text: object) -> str:
    return re.sub(r"([_\*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text or ""))

def display(value: object, fallback: str = "자료 부족") -> str:
    return str(value) if value is not None and value != "" else fallback

def format_won(value: object) -> str:
    """Format a stored price without inventing a zero or rounding real decimals."""
    if value is None or value == "" or isinstance(value, bool):
        return "자료 없음"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "자료 없음"
    if not number.is_finite():
        return "자료 없음"
    if number == number.to_integral_value():
        return f"{number:,.0f}원"
    rendered = f"{number:,f}".rstrip("0").rstrip(".")
    return f"{rendered}원"

def format_saved_time(value: object) -> str:
    """Display the stored wall-clock value to seconds without timezone conversion."""
    if value is None or value == "":
        return "자료 없음"
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "자료 없음"

def as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []

def split_messages(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    body_limit = max(1, limit - 16)
    remaining = text
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= body_limit:
            chunks.append(remaining)
            break
        cut = max(remaining.rfind("\n", 0, body_limit + 1), remaining.rfind(" ", 0, body_limit + 1))
        if cut <= 0:
            cut = body_limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    count = len(chunks)
    return [f"[{index}/{count}]\n{chunk}" for index, chunk in enumerate(chunks, 1)]

def format_briefing(result: dict[str, object]) -> str:
    kind=str(result.get("briefing_type")); analysis=result.get("analysis") if isinstance(result.get("analysis"),dict) else {}; decision=analysis.get("decision") if isinstance(analysis.get("decision"),dict) else {}
    title={"pre_market":"[QZ 장전 브리핑 | 09:00]","intraday_10am":"[QZ 오전 10시 브리핑]","market_close":"[QZ 장마감 브리핑 | 15:40]"}.get(kind,"[QZ 브리핑]")
    if result.get("status")=="no_market_open": return f"{title}\n장이 개시되지 않아 오늘 { {'pre_market':'장전','intraday_10am':'오전 10시','market_close':'장마감'}.get(kind,'') } 브리핑이 없습니다."
    lines=[title,"",f"시장 결론: {display(decision.get('headline') or analysis.get('summary'))}",f"신뢰도: {display(decision.get('confidence'))}/100",f"위험 수준: {display(decision.get('risk_level'))}"]
    if kind=="pre_market": lines += ["","장전 예상체결은 실제 외국인·기관 수급이 아닙니다.","개장 후 확인:"]
    elif kind=="intraday_10am": lines += ["","장전 예상과 실제 개장 후 수급의 차이를 확인합니다.","실제 수급은 저장된 공식 수집값 기준입니다."]
    elif kind=="market_close":
        close_analysis=result.get('market_close_analysis') if isinstance(result.get('market_close_analysis'),dict) else {}
        lines += ["",f"장전 판단 평가: {display(close_analysis.get('pre_market_evaluation'))}",f"10시 판단 평가: {display(close_analysis.get('intraday_evaluation'))}"]
    confirmation=[display(item, "") for item in as_list(decision.get('confirmation_conditions'))[:5] if display(item, "")]
    invalidation=[display(item, "") for item in as_list(decision.get('invalidation_conditions'))[:5] if display(item, "")]
    lines += [f"유지 조건: {', '.join(confirmation) or '자료 확인'}",f"위험 조건: {', '.join(invalidation) or '자료 확인'}"]
    holdings=result.get("holdings_analysis") if isinstance(result.get("holdings_analysis"),dict) else {}
    holding_rows=as_list(holdings.get("holdings"))
    urgent=sorted([x for x in holding_rows if isinstance(x,dict)],key=lambda x:x.get("priority") if isinstance(x.get("priority"),(int,float)) else 8)[:5]
    if urgent:
        lines += ["","보유종목 긴급 확인:"]+[f"- {x.get('name') or '종목명 미확인'}({x.get('code') or '코드 미확인'}): {(x.get('decision') if isinstance(x.get('decision'),dict) else {}).get('action_level') or '자료 부족'}" for x in urgent]
    leadership=result.get("leadership") if isinstance(result.get("leadership"),dict) else {}
    leaders=[]
    for section in ("kospi", "kosdaq", "rebound_candidates"):
        section_values=leadership.get(section) if isinstance(leadership.get(section),list) else []
        leaders.extend(value for value in section_values if isinstance(value,dict))
    if leaders:
        lines += ["", "주도주·반등 후보:"] + [f"- {item.get('name') or '종목명 미확인'}({item.get('code') or '코드 미확인'}): {item.get('score') if item.get('score') is not None else '점수 자료 부족'}" for item in leaders[:5]]
    recommendations=result.get("daily_recommendations") if isinstance(result.get("daily_recommendations"),dict) else {}
    strong=as_list(recommendations.get("strong")); review=as_list(recommendations.get("review"))
    if strong:
        lines += ["", "[오늘의 최우선 후보]"]
        for item in strong[:3]:
            if isinstance(item,dict): lines.append(f"- {item.get('name') or '종목명 미확인'}({item.get('code') or '코드 미확인'}) {display(item.get('total_score'))}점 / {', '.join(as_list(item.get('reasons'))[:2]) or '근거 자료 부족'} / 위험: {(as_list(item.get('risks'))[:1] or ['확인된 중대 위험 없음'])[0]} / 추격금지: {'예' if item.get('chase_buying_prohibited') else '아니오'}")
    if review:
        lines += ["", "[추가 검토 후보]"]
        for item in review[:3]:
            if isinstance(item,dict): lines.append(f"- {item.get('name') or '종목명 미확인'}({item.get('code') or '코드 미확인'}) {display(item.get('total_score'))}점 / 확인: {', '.join(as_list(item.get('missing'))[:2]) or '조건 유지 확인'}")
    if recommendations and not strong and not review: lines += ["", "오늘은 주봉 5주선 및 종합 기준을 충족한 추천 후보가 없습니다."]
    lines += ["","확정적인 매수·매도 지시가 아니며 조건 확인용입니다.",f"생성시각: {display(result.get('completed_at'), '-')}"]
    return mask_sensitive("\n".join(lines))

def format_daily_recommendation(report: dict[str, object]) -> str:
    """Format a persisted operational recommendation using shared safety helpers."""
    def rows(name: str) -> list[dict[str, object]]:
        value = report.get(name)
        return [item for item in value[:3] if isinstance(item, dict)] if isinstance(value, list) else []

    strong, review = rows("strong"), rows("review")
    lines = [
        f"[큐지 브리핑] 일일 추천 {display(report.get('trading_date'), '-')}", "",
        f"전체 검토 종목: {display(report.get('universe_input_count', report.get('input_count')), '0')}",
        f"주봉 MA5 통과 종목: {display(report.get('hard_filter_eligible_count', report.get('hard_filter_pass_count')), '0')}",
        f"완전 강추: {len(strong)}개", f"추가 검토: {len(review)}개",
        f"데이터 저장 시각: {format_saved_time(report.get('data_as_of'))}",
    ]
    for title, values in (("완전 강추", strong), ("추가 검토", review)):
        lines.extend(["", f"[{title}]"])
        if not values:
            lines.append("- 기준 충족 종목 없음")
        for row in values:
            reasons = ", ".join(map(str, as_list(row.get("reasons"))[:2])) or "근거 자료 부족"
            risks = ", ".join(map(str, as_list(row.get("risks"))[:2])) or "확인된 중대 위험 없음"
            missing = ", ".join(map(str, as_list(row.get("missing"))[:2])) or "별도 부족 자료 없음"
            lines.extend([
                f"- {display(row.get('name'), '종목명 자료 부족')}({display(row.get('code'), '코드 자료 부족')}) · {display(row.get('total_score'), '-')}점",
                f"  주봉 종가 / MA5: {format_won(row.get('weekly_close'))} / {format_won(row.get('weekly_ma5'))}",
                f"  핵심 근거: {reasons}", f"  주요 위험: {risks}", f"  부족 자료: {missing}",
            ])
    lines.extend(["", "자동매매 신호가 아닙니다.", "추격매수는 금지하거나 각별히 주의해야 합니다.",
                  "실제 주문 여부는 사용자가 직접 판단합니다.", f"저장된 분석 시점: {format_saved_time(report.get('data_as_of'))}"])
    return mask_sensitive("\n".join(lines))


def format_historical_daily_recommendation_test(report: dict[str, object]) -> str:
    """Make an unmistakable opt-in test message for persisted historical data."""
    normal = format_daily_recommendation(report).splitlines()
    body = normal[1:] if normal else []
    heading = [
        f"[큐지 브리핑 테스트·과거자료] 일일 추천 {display(report.get('trading_date'), '-')}",
        "테스트 전송", "과거 자료", f"기준일 {display(report.get('trading_date'), '-')}", "실시간 추천 아님",
    ]
    return mask_sensitive("\n".join(heading + body))


def format_runtime_alert(message: str, occurred_at: str) -> str:
    return mask_sensitive(f"[QZ 운영 경고]\n{message}\nPC와 키움 로그인 상태를 확인해야 합니다.\n발생시각: {occurred_at}")

def format_daily_summary(summary: dict[str, object]) -> str:
    briefs=summary.get("briefings") if isinstance(summary.get("briefings"),dict) else {}
    return "\n".join(["[QZ 일일 운영 결과]","",f"운영 결과: {summary.get('overall_result','unknown')}",f"자동로그인: {summary.get('automatic_login_result','unknown')}",f"09:00 장전: {briefs.get('pre_market','미완료')}",f"10:00 장중: {briefs.get('intraday_10am','미완료')}",f"15:40 장마감: {briefs.get('market_close','미완료')}","",f"연결 끊김: {summary.get('connection_drop_count',0)}회",f"TR timeout: {summary.get('tr_timeout_count',0)}회",f"브리핑 복구: {summary.get('briefing_recovery_count',0)}회",f"경고: {summary.get('warning_count',0)}건",f"오류: {summary.get('error_count',0)}건","","프로그램은 정상 종료됩니다."])
