from __future__ import annotations

from .models import DailyRecommendationReport


LABELS={"weekly_settlement":"주봉 5주선 안착","bottom_rebound":"바닥 반등","fund_inflow":"큰 자금 유입","daily_trend":"일봉 추세","catalyst":"재료·실적","liquidity":"유동성"}


def render_recommendations(report: DailyRecommendationReport) -> str:
    lines=["# 국장 일일 추천 후보", "", f"기준 시각: {report.as_of.isoformat()}", f"입력 {report.input_count}개 / 주봉 하드 필터 통과 {report.hard_filter_pass_count}개"]
    for title,rows in (("완전 강추",report.strong),("강추·추가 검토",report.review)):
        lines += ["",f"## {title}"]
        if not rows: lines.append("선정 기준 충족 종목 없음")
        for recommendation in rows:
            score=recommendation.score; signal=score.weekly; features=score.features
            lines += ["",f"{recommendation.rank}. {score.item.name}({score.item.code}) — {score.total_score:.2f}점",f"- 시장: {score.item.market} / 데이터 신뢰도: {score.confidence:.0%}"]
            if signal: lines.append(f"- 마지막 완성 주봉 종가 {signal.weekly_close:,.2f} / 5주선 {signal.weekly_ma5:,.2f} / 이격 {signal.distance_rate:+.2f}% / 연속 {signal.consecutive_weeks}주")
            if signal:
                slope = f"{signal.ma5_slope_rate:+.2f}%" if signal.ma5_slope_rate is not None else "산출 자료 부족"
                lines.append(f"- 5주선 기울기: {slope} / 위꼬리 비율: {signal.upper_wick_rate:.1%}")
            lines.append("- 핵심 근거: "+(", ".join(LABELS.get(reason,reason) for reason in score.reasons) or "확인 자료 부족"))
            lines.append(f"- 주봉 5주선 안착 평가: {score.components['weekly_settlement']:.2f}/20")
            lines.append(f"- 바닥 반등 평가: {score.components['bottom_rebound']:.2f}/20")
            lines.append(f"- 자금 유입 평가: {score.components['fund_inflow']:.2f}/25")
            lines.append(f"- 일봉 추세 평가: {score.components['daily_trend']:.2f}/15")
            flows=[]
            for label,value in (("외국인",features.foreign_flow),("기관",features.institution_flow),("프로그램",features.program_flow)):
                if value is not None: flows.append(f"{label} {value:+,.0f}")
            lines.append("- 수급 평가: "+(", ".join(flows) or "확인 자료 부족"))
            lines.append(f"- 재료·실적 평가: {score.components['catalyst']:.2f}/15")
            if features.catalyst_evidence:
                evidence = [f"{item.summary} ({item.source}, {item.observed_at.isoformat()})" for item in features.catalyst_evidence if item.source.strip() and item.observed_at <= features.as_of]
                if evidence: lines.append("- 재료 근거: "+"; ".join(evidence))
            lines.append(f"- 유동성·매매 적합성: {score.components['liquidity']:.2f}/5")
            lines.append(f"- 과열·위험 감점: -{score.risk_deduction:.2f}점")
            lines.append("- 추가 확인: "+(", ".join(score.missing) or "별도 확인 사항 없음"))
            lines.append("- 주요 위험: "+(", ".join(score.risks) or "확인된 중대 위험 없음"))
            lines.append(f"- 추격매수 금지: {'예' if score.risk_deduction >= 10 else '아니오'} / 선호 진입: {features.preferred_entry or '관찰 우선'}")
            lines.append("- 무효화 조건: "+(", ".join(features.invalidation_conditions) or "5주선 재이탈 여부 확인"))
            lines.append(f"- 관점: {features.horizon or '단기·스윙'}")
            lines.append(f"- 한 줄 요약: {', '.join(LABELS.get(reason,reason) for reason in score.reasons[:2]) or '추가 확인이 필요한 후보'} 중심의 조건부 후보입니다.")
    if report.excluded:
        lines += ["","## 제외 종목"]+[f"- {row['name']}({row['code']}): {row['reason']}" for row in report.excluded]
    if report.warnings: lines += ["","## 주의"]+[f"- {warning}" for warning in report.warnings]
    return "\n".join(lines)
