from __future__ import annotations

from datetime import datetime, timedelta

from .models import CatalystEvidence, RecommendationFeatures, RiskFlag, StockUniverseItem, WeeklyBar
from .renderer import render_recommendations
from .selector import select_recommendations


AS_OF=datetime(2026,7,24,16,0)


def weekly(closes: list[float], *, incomplete_close: float | None = None) -> tuple[WeeklyBar,...]:
    bars=[]
    start=AS_OF-timedelta(weeks=len(closes))
    for index,close in enumerate(closes):
        ended=start+timedelta(weeks=index)
        bars.append(WeeklyBar(ended,close*0.97,close*1.03,close*0.95,close,True))
    if incomplete_close is not None:
        bars.append(WeeklyBar(AS_OF+timedelta(days=1),incomplete_close,incomplete_close,incomplete_close,incomplete_close,False))
    return tuple(bars)


def feature(code:str,name:str,*,closes=None,bottom=.7,fund=.7,daily=.7,catalyst=.5,liquidity=.8,confidence=.85,risks=(),tradable=True,security_type="common_stock",missing=()):
    evidence=(CatalystEvidence("검증용 공개 근거","offline_fixture",AS_OF-timedelta(hours=1)),) if catalyst is not None else ()
    return RecommendationFeatures(
        StockUniverseItem("KOSPI" if int(code)%2 else "KOSDAQ",code,name,security_type,tradable,"거래정지" if not tradable else None),AS_OF,
        weekly(closes or [90,92,95,98,101,105]),bottom,fund,daily,catalyst,liquidity,confidence,1_000_000_000,
        catalyst_evidence=evidence,risks=tuple(risks),missing=tuple(missing),preferred_entry="눌림 대기",invalidation_conditions=("완성 주봉 종가의 5주선 재이탈",),
    )


def validation_features() -> list[RecommendationFeatures]:
    return [
        feature("100001","바닥자금형",bottom=.95,fund=.95,daily=.8,catalyst=.6),
        feature("100002","수급재료형",bottom=.2,fund=1,daily=.85,catalyst=1),
        feature("100003","무재료반등형",bottom=1,fund=.9,daily=.8,catalyst=None),
        feature("100004","과열검토형",bottom=.6,fund=.9,daily=.9,catalyst=.7,risks=(RiskFlag("overheated","단기간 급등 과열",18),)),
        feature("100005","주봉탈락형",closes=[110,108,105,102,99,95],bottom=1,fund=1,daily=1,catalyst=1),
        feature("100006","자료부족형",bottom=None,fund=.8,daily=None,catalyst=None,missing=("바닥 구조","일봉 추세","재료")),
        feature("100007","위험감점형",bottom=.9,fund=.9,daily=.9,catalyst=.9,risks=(RiskFlag("warning","투자경고 확인 필요",12),)),
        feature("100008","동점가",bottom=.7,fund=.7,daily=.7,catalyst=.5,confidence=.8),
        feature("100010","동점나",bottom=.7,fund=.7,daily=.7,catalyst=.5,confidence=.8),
        feature("100011","거래정지형",tradable=False,bottom=1,fund=1,daily=1,catalyst=1),
    ]


def validate_stock_recommendations() -> dict[str,object]:
    features=validation_features(); report=select_recommendations(features); rendered=render_recommendations(report)
    selected=report.strong+report.review
    underfilled=select_recommendations(features[:3])
    checks={
        "hard_filter":any(row["code"]=="100005" for row in report.excluded),
        "untradable_excluded":any(row["code"]=="100011" for row in report.excluded),
        "groups_disjoint":not ({r.score.item.code for r in report.strong}&{r.score.item.code for r in report.review}),
        "target_count":5 <= len(selected) <= 6,
        "underfilled_not_forced":len(underfilled.strong)+len(underfilled.review)<5 and bool(underfilled.warnings),
        "deterministic": [r.score.item.code for r in selected]==[r.score.item.code for r in select_recommendations(features).strong+select_recommendations(features).review],
        "safe_render":"None" not in rendered and "null" not in rendered,
    }
    return {"success":all(checks.values()),"checks":checks,"report":report,"rendered":rendered}


def print_stock_validation(result:dict[str,object])->None:
    report=result["report"]
    print(f"INPUT={report.input_count} HARD_FILTER_PASS={report.hard_filter_pass_count}")
    print("EXCLUDED="+", ".join(f"{row['code']}:{row['reason']}" for row in report.excluded))
    print("STRONG="+", ".join(f"{r.score.item.code}:{r.score.total_score:.2f}" for r in report.strong))
    print("REVIEW="+", ".join(f"{r.score.item.code}:{r.score.total_score:.2f}" for r in report.review))
    for recommendation in report.strong+report.review:
        score=recommendation.score
        print(f"  {score.item.code} reasons={','.join(score.reasons) or '자료 부족'} risk_deduction={score.risk_deduction:.2f} missing={','.join(score.missing) or '없음'}")
    tie_order=[r.score.item.code for r in report.strong+report.review if r.score.item.code in {"100008","100010"}]
    print("TIE_ORDER="+", ".join(tie_order))
    for name,passed in result["checks"].items(): print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print(f"STOCK RECOMMENDATION VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
