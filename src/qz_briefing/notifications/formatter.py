# -*- coding: utf-8 -*-
from __future__ import annotations
import re

ACCOUNT = re.compile(r"(?<!\d)\d{8,12}(?!\d)")

def mask_sensitive(text: str) -> str:
    return ACCOUNT.sub(lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:], text)

def escape_markdown(text: object) -> str:
    return re.sub(r"([_\*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text or ""))

def display(value: object, fallback: str = "자료 부족") -> str:
    return str(value) if value is not None and value != "" else fallback

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
    lines += ["","확정적인 매수·매도 지시가 아니며 조건 확인용입니다.",f"생성시각: {display(result.get('completed_at'), '-')}"]
    return mask_sensitive("\n".join(lines))

def format_runtime_alert(message: str, occurred_at: str) -> str:
    return mask_sensitive(f"[QZ 운영 경고]\n{message}\nPC와 키움 로그인 상태를 확인해야 합니다.\n발생시각: {occurred_at}")

def format_daily_summary(summary: dict[str, object]) -> str:
    briefs=summary.get("briefings") if isinstance(summary.get("briefings"),dict) else {}
    return "\n".join(["[QZ 일일 운영 결과]","",f"운영 결과: {summary.get('overall_result','unknown')}",f"자동로그인: {summary.get('automatic_login_result','unknown')}",f"09:00 장전: {briefs.get('pre_market','미완료')}",f"10:00 장중: {briefs.get('intraday_10am','미완료')}",f"15:40 장마감: {briefs.get('market_close','미완료')}","",f"연결 끊김: {summary.get('connection_drop_count',0)}회",f"TR timeout: {summary.get('tr_timeout_count',0)}회",f"브리핑 복구: {summary.get('briefing_recovery_count',0)}회",f"경고: {summary.get('warning_count',0)}건",f"오류: {summary.get('error_count',0)}건","","프로그램은 정상 종료됩니다."])
