from dataclasses import replace
from datetime import date, datetime, timedelta

from qz_briefing.recommendations.data_models import (
    DataMetadata, DailyBar, InvestorFlowSnapshot, RecommendationDataBundle, StockMasterRecord,
)
from qz_briefing.recommendations.data_pipeline import aggregate_weekly_bars, compute_price_features
from qz_briefing.recommendations.integrated_scoring import evaluate_preliminary_candidate, rank_preliminary_candidates


AS_OF = datetime(2026, 7, 23, 18)


def _bundle(code="800001", *, count=260, step=.2, flows=True, trading_value=5000.0):
    meta = DataMetadata(code, "가상종목", "KOSPI", AS_OF, "synthetic-fixture", AS_OF)
    master = StockMasterRecord(meta, "common_stock")
    rows = []
    day = date(2025, 7, 1)
    price = 100.0
    while len(rows) < count:
        if day.weekday() < 5:
            price = max(1, price + step)
            rows.append(DailyBar(meta, day, price-.3, price+.5, price-.6, price, 1000, trading_value, True))
        day += timedelta(days=1)
    daily = tuple(rows)
    weekly = aggregate_weekly_bars(daily, AS_OF)
    flow = InvestorFlowSnapshot(meta, tuple([100.0] * 20), tuple([80.0] * 20)) if flows else None
    return RecommendationDataBundle(master, daily, weekly, compute_price_features(daily, AS_OF), flow)


def test_completed_weekly_close_must_be_strictly_above_ma5():
    bundle = _bundle()
    assert evaluate_preliminary_candidate(bundle).weekly_filter_passed
    completed = [bar for bar in bundle.weekly_bars if bar.metadata.complete]
    equal = sum(bar.close for bar in completed[-5:-1]) / 4
    weekly = tuple(replace(bar, close=equal) if bar is completed[-1] else bar for bar in bundle.weekly_bars)
    result = evaluate_preliminary_candidate(replace(bundle, weekly_bars=weekly))
    assert not result.weekly_filter_passed


def test_incomplete_current_week_and_future_week_are_never_used():
    bundle = _bundle()
    last = bundle.weekly_bars[-1]
    manipulated = replace(last, close=last.close * 10, metadata=replace(last.metadata, complete=False))
    future = replace(manipulated, week_start=date(2027, 1, 4), week_end=date(2027, 1, 8), metadata=replace(last.metadata, complete=True))
    baseline = evaluate_preliminary_candidate(bundle)
    changed = evaluate_preliminary_candidate(replace(bundle, weekly_bars=bundle.weekly_bars[:-1] + (manipulated, future)))
    assert changed.weekly_filter_passed == baseline.weekly_filter_passed
    assert changed.weekly_score == baseline.weekly_score


def test_fewer_than_five_completed_weeks_fails():
    bundle = _bundle(count=20)
    assert not evaluate_preliminary_candidate(bundle).weekly_filter_passed


def test_existing_fund_flow_score_is_connected_and_missing_is_zero():
    strong = evaluate_preliminary_candidate(_bundle())
    missing = evaluate_preliminary_candidate(_bundle(flows=False))
    assert strong.fund_flow_score > missing.fund_flow_score == 0


def test_extreme_low_liquidity_gets_low_score_and_risk_penalty():
    result = evaluate_preliminary_candidate(_bundle(trading_value=20))
    assert result.liquidity_score == 0
    assert "극단적 저유동성" in result.risk_flags
    assert result.risk_penalty >= 5


def test_overextended_weekly_price_scores_less_than_fresh_breakout():
    fresh = _bundle(step=.05)
    completed = [bar for bar in fresh.weekly_bars if bar.metadata.complete]
    last = completed[-1]
    stretched = replace(fresh, weekly_bars=tuple(replace(bar, close=bar.close * 1.5, high=bar.high * 1.5) if bar is last else bar for bar in fresh.weekly_bars))
    assert evaluate_preliminary_candidate(stretched).weekly_score < evaluate_preliminary_candidate(fresh).weekly_score


def test_ranking_excludes_hard_filter_failures_and_does_not_fill():
    passed = _bundle("800001")
    failed = _bundle("800002", step=-.1)
    ranked, excluded = rank_preliminary_candidates((failed, passed), limit=5)
    assert [row.item.code for row in ranked] == ["800001"]
    assert [row.item.code for row in excluded] == ["800002"]


def test_tie_sort_uses_code_as_final_deterministic_key():
    first = _bundle("800002")
    second = _bundle("800001")
    one, _ = rank_preliminary_candidates((first, second))
    two, _ = rank_preliminary_candidates((second, first))
    assert [x.item.code for x in one] == [x.item.code for x in two] == ["800001", "800002"]


def test_scores_stay_in_declared_ranges_and_final_has_floor():
    result = evaluate_preliminary_candidate(_bundle())
    assert 0 <= result.weekly_score <= 20
    assert 0 <= result.bottom_reversal_score <= 20
    assert 0 <= result.fund_flow_score <= 25
    assert 0 <= result.daily_trend_score <= 15
    assert 0 <= result.liquidity_score <= 5
    assert 0 <= result.final_total_score <= 100
    assert result.catalyst_score == 0 and result.catalyst_status == "not_evaluated"
