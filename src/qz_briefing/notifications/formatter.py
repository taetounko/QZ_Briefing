# -*- coding: utf-8 -*-
from __future__ import annotations
import re

ACCOUNT = re.compile(r"(?<!\d)\d{8,12}(?!\d)")

def mask_sensitive(text: str) -> str:
    return ACCOUNT.sub(lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:], text)

def escape_markdown(text: object) -> str:
    return re.sub(r"([_\*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text or ""))

def split_messages(text: str, limit: int = 3800) -> list[str]:
    paragraphs = text.splitlines(); chunks=[]; current=[]; size=0
    for line in paragraphs:
        if current and size + len(line) + 1 > limit:
            chunks.append("\n".join(current)); current=[]; size=0
        if len(line) > limit:
            words=line.split(); piece=[]
            for word in words:
                if piece and len(" ".join(piece+[word])) > limit: chunks.append(" ".join(piece)); piece=[]
                piece.append(word)
            if piece: current.append(" ".join(piece)); size=len(current[-1])
        else: current.append(line); size += len(line)+1
    if current: chunks.append("\n".join(current))
    if len(chunks)>1: return [f"[{i}/{len(chunks)}]\n{chunk}" for i,chunk in enumerate(chunks,1)]
    return chunks or [""]

def format_briefing(result: dict[str, object]) -> str:
    kind=str(result.get("briefing_type")); analysis=result.get("analysis") if isinstance(result.get("analysis"),dict) else {}; decision=analysis.get("decision") if isinstance(analysis.get("decision"),dict) else {}
    title={"pre_market":"[QZ 장전 브리핑 | 09:00]","intraday_10am":"[QZ 오전 10시 브리핑]","market_close":"[QZ 장마감 브리핑 | 15:40]"}.get(kind,"[QZ 브리핑]")
    if result.get("status")=="no_market_open": return f"{title}\n장이 개시되지 않아 오늘 { {'pre_market':'장전','intraday_10am':'오전 10시','market_close':'장마감'}.get(kind,'') } 브리핑이 없습니다."
    lines=[title,"",f"시장 결론: {decision.get('headline') or analysis.get('summary','자료 부족')}",f"신뢰도: {decision.get('confidence',0)}/100",f"위험 수준: {decision.get('risk_level','unknown')}"]
    if kind=="pre_market": lines += ["","장전 예상체결은 실제 외국인·기관 수급이 아닙니다.","개장 후 확인:"]
    elif kind=="intraday_10am": lines += ["","장전 예상과 실제 개장 후 수급의 차이를 확인합니다.","실제 수급은 저장된 공식 수집값 기준입니다."]
    elif kind=="market_close": lines += ["",f"장전 판단 평가: {result.get('market_close_analysis',{}).get('pre_market_evaluation','자료 부족') if isinstance(result.get('market_close_analysis'),dict) else '자료 부족'}",f"10시 판단 평가: {result.get('market_close_analysis',{}).get('intraday_evaluation','자료 부족') if isinstance(result.get('market_close_analysis'),dict) else '자료 부족'}"]
    lines += [f"유지 조건: {', '.join(decision.get('confirmation_conditions',[])[:5]) or '자료 확인'}",f"위험 조건: {', '.join(decision.get('invalidation_conditions',[])[:5]) or '자료 확인'}"]
    holdings=result.get("holdings_analysis") if isinstance(result.get("holdings_analysis"),dict) else {}; urgent=sorted([x for x in holdings.get("holdings",[]) if isinstance(x,dict)],key=lambda x:x.get("priority",8))[:5]
    lines += ["","보유종목 긴급 확인:"]+[f"- {x.get('name')}({x.get('code')}): {x.get('decision',{}).get('action_level','자료 부족')}" for x in urgent]
    lines += ["","확정적인 매수·매도 지시가 아니며 조건 확인용입니다.",f"생성시각: {result.get('completed_at','-')}"]
    return mask_sensitive("\n".join(lines))

def format_runtime_alert(message: str, occurred_at: str) -> str:
    return mask_sensitive(f"[QZ 운영 경고]\n{message}\nPC와 키움 로그인 상태를 확인해야 합니다.\n발생시각: {occurred_at}")

def format_daily_summary(summary: dict[str, object]) -> str:
    briefs=summary.get("briefings") if isinstance(summary.get("briefings"),dict) else {}
    return "\n".join(["[QZ 일일 운영 결과]","",f"운영 결과: {summary.get('overall_result','unknown')}",f"자동로그인: {summary.get('automatic_login_result','unknown')}",f"09:00 장전: {briefs.get('pre_market','미완료')}",f"10:00 장중: {briefs.get('intraday_10am','미완료')}",f"15:40 장마감: {briefs.get('market_close','미완료')}","",f"연결 끊김: {summary.get('connection_drop_count',0)}회",f"TR timeout: {summary.get('tr_timeout_count',0)}회",f"브리핑 복구: {summary.get('briefing_recovery_count',0)}회",f"경고: {summary.get('warning_count',0)}건",f"오류: {summary.get('error_count',0)}건","","프로그램은 정상 종료됩니다."])
