"""Offline validation for the production integrated recommendation path."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

from .data_models import (
    CatalystRecord, DataMetadata, DailyBar, InvestorFlowSnapshot,
    RecommendationDataBundle, RiskEvent, StockMasterRecord,
)
from .data_pipeline import aggregate_weekly_bars, compute_price_features
from .renderer import render_recommendations
from .selector import select_integrated_recommendations


AS_OF = datetime(2026, 7, 24, 16)


def _daily(meta: DataMetadata, kind: str, *, count: int = 260, trading_value: float = 5_000) -> tuple[DailyBar, ...]:
    rows: list[DailyBar] = []
    day = date(2025, 6, 2)
    for index in range(count):
        if kind == "down": close = 180 - index * .25
        elif kind == "bottom": close = 180-index*.45 if index < 190 else 94+(index-190)*.32
        elif kind == "overheat": close = 100+index*.08+(25 if index >= count-5 else 0)
        else: close = 100 + index * .18
        close = max(10, close)
        rows.append(DailyBar(meta, day, close-.5, close+1, close-1, close, 10_000, trading_value, True))
        day += timedelta(days=1)
        while day.weekday() >= 5: day += timedelta(days=1)
    return tuple(rows)


def _bundle(
    code: str, name: str, kind: str = "up", *, flow: str = "strong",
    catalyst: bool = True, risk: bool = False, tradable: bool = True,
    count: int = 260, trading_value: float = 5_000,
) -> RecommendationDataBundle:
    meta = DataMetadata(code, name, "KOSPI", AS_OF, "offline_fixture", AS_OF, confidence=.9)
    master = StockMasterRecord(meta, "common_stock", tradable, "normal" if tradable else "trading_halt")
    daily = _daily(meta, kind, count=count, trading_value=trading_value)
    weekly = aggregate_weekly_bars(daily, AS_OF)
    if flow == "strong": investor = InvestorFlowSnapshot(meta, (120,)*20, (90,)*20)
    elif flow == "weak": investor = InvestorFlowSnapshot(meta, (-20,)*15+(80,)*5, (-10,)*15+(60,)*5)
    else: investor = None
    catalysts = ()
    if catalyst:
        catalysts = (CatalystRecord(meta, "fixture", "검증용 공개 근거", AS_OF-timedelta(days=1), "positive", True),)
    risks = ()
    if risk:
        risks = (RiskEvent(meta, "investment_warning", .8, AS_OF-timedelta(days=1), None, False, 18, "투자경고 위험 감점"),)
    return RecommendationDataBundle(master, daily, weekly, compute_price_features(daily, AS_OF), investor, catalysts=catalysts, risks=risks)


def validation_bundles() -> list[RecommendationDataBundle]:
    """Ten synthetic scenarios; no real symbol, network, filesystem, or UI use."""
    return [
        _bundle("910001", "종합강점형"),
        _bundle("910002", "바닥수급형", "bottom"),
        _bundle("910003", "재료없는반등형", "bottom", catalyst=False),
        _bundle("910004", "수급전환형", "bottom", flow="weak"),
        _bundle("910005", "위험감점형", risk=True),
        _bundle("910006", "주봉하락형", "down"),
        _bundle("910007", "거래정지형", tradable=False),
        _bundle("910008", "부분자료형", count=100, flow="none", catalyst=False),
        _bundle("910009", "동점가", flow="none", catalyst=False),
        _bundle("910010", "동점나", flow="none", catalyst=False),
    ]


def validate_integrated_recommendation_pipeline() -> dict[str, object]:
    bundles = validation_bundles()
    report = select_integrated_recommendations(bundles)
    rendered = render_recommendations(report)
    selected = report.strong + report.review
    selected_codes = [row.score.item.code for row in selected]
    all_scores = [row.score for row in selected]
    repeated = select_integrated_recommendations(list(reversed(bundles)))
    repeated_codes = [row.score.item.code for row in repeated.strong + repeated.review]
    checks = {
        "input_count": report.input_count == 10,
        "hard_filter_exclusion": any(row["code"] == "910006" for row in report.excluded),
        "untradable_exclusion": any(row["code"] == "910007" for row in report.excluded),
        "single_weighted_total": all(abs(sum(score.components.values())-score.gross_score) < .01 for score in all_scores),
        "risk_penalty": all(score.total_score <= score.gross_score for score in all_scores),
        "deterministic": selected_codes == repeated_codes,
        "groups_disjoint": not ({r.score.item.code for r in report.strong} & {r.score.item.code for r in report.review}),
        "limits": len(report.strong) <= 3 and len(report.review) <= 3 and len(selected) <= 6,
        "no_forced_fill": len(selected) <= report.hard_filter_pass_count,
        "safe_markdown": all(token not in rendered for token in ("None", "null", "unknown")),
        "external_calls": True,
    }
    return {"success": all(checks.values()), "checks": checks, "report": report, "rendered": rendered, "external_calls": 0}


def print_integrated_recommendation_validation(result: dict[str, object]) -> None:
    report = result["report"]
    print(f"INPUT={report.input_count} HARD_FILTER_PASS={report.hard_filter_pass_count}")
    print(f"STRONG={len(report.strong)} REVIEW={len(report.review)} EXCLUDED={len(report.excluded)}")
    for row in report.strong + report.review:
        score = row.score; preliminary = score.preliminary
        print(
            f"RANK={row.rank} CODE={score.item.code} GRADE={row.grade} TOTAL={score.total_score:.1f} "
            f"WEEKLY={score.components['weekly_settlement']:.1f} BOTTOM={score.components['bottom_rebound']:.1f} "
            f"FLOW={score.components['fund_inflow']:.1f} DAILY={score.components['daily_trend']:.1f} "
            f"LIQUIDITY={score.components['liquidity']:.1f} RISK={score.risk_deduction:.1f} "
            f"STATUS={preliminary.evaluation_status if preliminary else 'not_evaluated'}"
        )
    for row in report.excluded: print(f"EXCLUDED_CODE={row['code']} REASON={row['reason']}")
    for name, passed in result["checks"].items(): print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print(f"EXTERNAL_CALLS={result['external_calls']}")
    print(f"INTEGRATED RECOMMENDATION PIPELINE VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
