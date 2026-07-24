from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from qz_briefing.__main__ import run
from qz_briefing.recommendations.data_cache import RecommendationDataCache
from qz_briefing.recommendations.data_models import (
    CatalystRecord, CollectionFailure, DataMetadata, DailyBar, DataQualityReport, FundamentalSnapshot, InvestorFlowSnapshot,
    RecommendationDataBundle, RiskEvent, StockMasterRecord,
)
from qz_briefing.recommendations.data_pipeline import (
    aggregate_weekly_bars, compute_price_features, normalize_daily_bars,
    compute_flow_features, to_recommendation_features, universe_decision,
    weekly_ma5_metrics,
)
from qz_briefing.recommendations.data_validation import AS_OF, validate_recommendation_data_pipeline
from qz_briefing.recommendations.request_planner import (
    CacheState, CollectionMode, CollectionPolicy, PreliminaryCandidate,
    build_request_plan, select_flow_candidates,
)
from qz_briefing.recommendations.scoring import evaluate_candidate


def master(code="800001",market="KOSPI",kind="common_stock",tradable=True,status="normal",risks=()):
    return StockMasterRecord(DataMetadata(code,"가상종목",market,AS_OF,"fixture",AS_OF,True,False,.9),kind,tradable,status,risks)


def bars(item=None,count=130,start=date(2026,1,1),adjusted=True):
    item=item or master(); output=[]; day=start; price=100.0
    while len(output)<count:
        if day.weekday()<5:
            price+=.2; meta=DataMetadata(item.metadata.code,item.metadata.name,item.metadata.market,AS_OF,"fixture",AS_OF,True,False,.9)
            output.append(DailyBar(meta,day,price-.5,price+1,price-1,price,1000+len(output)*10,(1000+len(output)*10)*price,adjusted))
        day+=timedelta(days=1)
    return output


@pytest.mark.parametrize(("market","accepted"),[("KOSPI",True),("KOSDAQ",True),("",False)])
def test_market_universe(market,accepted): assert universe_decision(master(market=market))[0] is accepted


@pytest.mark.parametrize("kind",["etf","etn","reit","spac","preferred"])
def test_instrument_types_are_excluded(kind): assert not universe_decision(master(kind=kind))[0]


def test_untradable_is_excluded_but_managed_tradable_is_kept():
    assert not universe_decision(master(tradable=False,status="trading_halt"))[0]
    assert universe_decision(master(status="managed",risks=("managed",)))[0]


def test_daily_normalization_sorts_deduplicates_and_rejects_future():
    raw=bars(count=3); duplicate=replace(raw[1],close=raw[1].close+.1,metadata=replace(raw[1].metadata,updated_at=AS_OF+timedelta(seconds=1)))
    future=replace(raw[-1],trading_date=AS_OF.date()+timedelta(days=1))
    normalized,errors=normalize_daily_bars([raw[2],raw[0],raw[1],duplicate,future],AS_OF)
    assert [row.trading_date for row in normalized]==sorted({row.trading_date for row in raw})
    assert normalized[1].close==duplicate.close and len(errors)==2


def test_invalid_ohlc_and_negative_volume_are_rejected():
    raw=bars(count=2); bad_ohlc=replace(raw[0],low=raw[0].high+1); negative=replace(raw[1],volume=-1)
    normalized,errors=normalize_daily_bars([bad_ohlc,negative],AS_OF)
    assert not normalized and len(errors)==2


def test_adjusted_and_unadjusted_series_cannot_mix():
    raw=bars(count=2)
    with pytest.raises(ValueError,match="수정주가"): normalize_daily_bars([raw[0],replace(raw[1],adjusted=False)],AS_OF)


def test_weekly_aggregation_and_short_holiday_week():
    raw=bars(count=8,start=date(2026,5,4)); normalized,_=normalize_daily_bars(raw,AS_OF)
    latest=normalized[-1]; iso=latest.trading_date.isocalendar()
    weekly=aggregate_weekly_bars(normalized,AS_OF,week_last_trading_days={(iso.year,iso.week):latest.trading_date})
    first=[row for row in normalized if row.trading_date.isocalendar()[:2]==weekly[0].week_start.isocalendar()[:2]]
    assert weekly[0].open==first[0].open and weekly[0].close==first[-1].close
    assert weekly[0].volume==sum(row.volume for row in first)


def test_current_week_incomplete_until_actual_last_session_close():
    as_of=datetime(2026,7,23,12); raw=bars(count=1,start=date(2026,7,23)); normalized,_=normalize_daily_bars(raw,as_of)
    key=normalized[-1].trading_date.isocalendar(); mapping={(key.year,key.week):normalized[-1].trading_date}
    assert not aggregate_weekly_bars(normalized,as_of,week_last_trading_days=mapping)[-1].metadata.complete
    assert aggregate_weekly_bars(normalized,as_of.replace(hour=16),week_last_trading_days=mapping)[-1].metadata.complete


def test_only_completed_weekly_bars_reach_ma5_engine():
    raw=bars(); normalized,_=normalize_daily_bars(raw,AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    bundle=RecommendationDataBundle(master(),normalized,weekly,compute_price_features(normalized,AS_OF))
    converted=to_recommendation_features(bundle)
    assert sum(bar.completed for bar in converted.weekly_bars)>=5
    assert not converted.weekly_bars[-1].completed


def test_indicators_and_price_position_are_calculated():
    normalized,_=normalize_daily_bars(bars(),AS_OF); result=compute_price_features(normalized,AS_OF).values
    for key in ("ma5","ma20","ma60","rsi14","macd_macd","atr14","high52","low52","position52","volume_surge","trading_value_surge","obv","cmf20"):
        assert key in result


def test_weekly_ma5_metrics_use_completed_bars_only():
    normalized,_=normalize_daily_bars(bars(),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    metrics=weekly_ma5_metrics(weekly)
    assert metrics and metrics["completed"] and metrics["weekly_close_above_ma5"]
    changed=weekly[:-1]+(replace(weekly[-1],close=1),)
    assert weekly_ma5_metrics(changed)==metrics


def test_flow_features_include_5_20_day_sums_and_streak():
    result=compute_flow_features(tuple(range(1,22)))
    assert result["sum5"]==sum(range(17,22)) and result["sum20"]==sum(range(2,22))
    assert result["consecutive_net_buy_days"]==21


def test_negative_flow_streak_is_distinct():
    result=compute_flow_features((1,-1,-2,-3))
    assert result["consecutive_net_sell_days"]==3 and result["consecutive_net_buy_days"]==0


def test_missing_flow_features_remain_missing(): assert compute_flow_features(())=={}


def test_insufficient_indicator_history_reports_missing_not_zero():
    normalized,_=normalize_daily_bars(bars(count=5),AS_OF); result=compute_price_features(normalized,AS_OF)
    assert "ma20" in result.missing and "rsi14" in result.missing and "ma20" not in result.values


def test_trading_value_missing_is_reported():
    raw=[replace(item,trading_value=None) for item in bars()]; normalized,_=normalize_daily_bars(raw,AS_OF)
    assert "trading_value" in compute_price_features(normalized,AS_OF).missing


def test_future_completed_bar_is_never_normalized():
    raw=bars(count=2); raw.append(replace(raw[-1],trading_date=AS_OF.date()+timedelta(days=10)))
    normalized,_=normalize_daily_bars(raw,AS_OF)
    assert all(item.trading_date<=AS_OF.date() for item in normalized)


def test_invalid_code_is_excluded(): assert not universe_decision(master(code="ABC"))[0]


def test_non_target_market_is_excluded(): assert not universe_decision(master(market="KONEX"))[0]


def test_cache_supports_every_required_kind(tmp_path):
    cache=RecommendationDataCache(tmp_path)
    for kind in cache.KINDS:
        assert cache.save(kind,"key",{},as_of=AS_OF,source="fixture").parent.name==kind


def test_request_plan_excludes_account_and_order_trs():
    candidate=PreliminaryCandidate("800001",80,.9,True)
    plan=build_request_plan([CacheState("800001",kind,"missing") for kind in ("daily","flow")],candidates=[candidate])
    assert {item.operation for item in plan.requests}<={"GetMaster","OPT10081","OPT10059"}


def test_request_plan_exposes_interval_and_overload_backoff():
    plan=build_request_plan([CacheState("800001","daily","missing")])
    assert plan.minimum_interval_ms==1000 and plan.overload_backoff_ms==(3000,7000,15000)


def test_request_plan_counts_new_and_stale():
    plan=build_request_plan([CacheState("800001","daily","missing",new_listing=True),CacheState("800002","daily","stale")])
    assert plan.new_count==1 and plan.stale_count==1 and plan.counts_by_kind["daily"]==2


def test_unverified_catalyst_is_not_scored():
    item=master(); normalized,_=normalize_daily_bars(bars(item),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    catalyst=CatalystRecord(item.metadata,"news","미검증 fixture",AS_OF-timedelta(hours=1),verified=False)
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF),catalysts=(catalyst,)))
    assert converted.catalyst_strength is None and not converted.catalyst_evidence


def test_future_catalyst_is_not_scored():
    item=master(); normalized,_=normalize_daily_bars(bars(item),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    catalyst=CatalystRecord(item.metadata,"news","미래 fixture",AS_OF+timedelta(hours=1),verified=True)
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF),catalysts=(catalyst,)))
    assert converted.catalyst_strength is None


def test_source_less_catalyst_is_not_scored():
    item=master(); normalized,_=normalize_daily_bars(bars(item),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    catalyst=CatalystRecord(replace(item.metadata,source=""),"news","무출처 fixture",AS_OF-timedelta(hours=1),verified=True)
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF),catalysts=(catalyst,)))
    assert converted.catalyst_strength is None


def test_data_quality_success_rate_and_failures_are_explicit():
    failure=CollectionFailure("800001","daily","fixture failure",AS_OF,1)
    report=DataQualityReport(10,8,2,[failure],["partial data"])
    assert report.success_rate==.8 and report.failures[0].retry_count==1


def test_less_than_120_daily_bars_is_excluded_from_engine():
    item=master(); normalized,_=normalize_daily_bars(bars(item,count=119),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF)))
    assert not evaluate_candidate(converted).eligible and "일봉 120거래일" in converted.missing


def test_master_warning_becomes_deduction_not_hard_exclusion():
    item=master(risks=("investment_warning",)); normalized,_=normalize_daily_bars(bars(item),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF)))
    assert converted.risks[0].deduction==8 and evaluate_candidate(converted).eligible


def test_sourced_fundamental_snapshot_becomes_evidence():
    item=master(); normalized,_=normalize_daily_bars(bars(item),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    fundamental=FundamentalSnapshot(item.metadata,"2026Q2",revenue_growth=.2)
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF),fundamentals=(fundamental,)))
    assert converted.catalyst_strength==item.metadata.confidence
    assert converted.catalyst_evidence[0].summary=="실적 2026Q2"


def test_missing_flow_and_catalyst_are_allowed_and_reduce_confidence():
    normalized,_=normalize_daily_bars(bars(),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    converted=to_recommendation_features(RecommendationDataBundle(master(),normalized,weekly,compute_price_features(normalized,AS_OF)))
    assert converted.fund_inflow is None and converted.catalyst_strength is None
    assert "종목별 투자자 수급" in converted.missing and converted.confidence<=.7


def test_valid_flow_catalyst_and_risks_convert_without_fabrication():
    item=master(); normalized,_=normalize_daily_bars(bars(item),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    meta=item.metadata; flow=InvestorFlowSnapshot(meta,(1,2,3),(1,1,1))
    catalyst=CatalystRecord(meta,"earnings","검증 fixture",AS_OF-timedelta(hours=1),"positive",True,AS_OF+timedelta(days=1))
    risk=RiskEvent(meta,"managed",.5,AS_OF-timedelta(days=1),None,False,7,"관리 위험")
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF),flow,None,(),(catalyst,),(risk,)))
    assert converted.fund_inflow is not None and converted.catalyst_strength==.9
    assert converted.risks[0].deduction==7 and evaluate_candidate(converted).eligible


def test_hard_risk_becomes_untradable():
    item=master(); normalized,_=normalize_daily_bars(bars(item),AS_OF); weekly=aggregate_weekly_bars(normalized,AS_OF)
    risk=RiskEvent(item.metadata,"trading_halt",1,AS_OF,None,True,100,"거래정지")
    converted=to_recommendation_features(RecommendationDataBundle(item,normalized,weekly,compute_price_features(normalized,AS_OF),risks=(risk,)))
    assert not evaluate_candidate(converted).eligible


def test_cache_atomic_roundtrip_staleness_and_corruption(tmp_path):
    cache=RecommendationDataCache(tmp_path); path=cache.save("daily","800001",{"rows":1},as_of=AS_OF,source="fixture")
    assert path.exists() and not list(path.parent.glob("*.tmp"))
    assert cache.load("daily","800001",now=AS_OF,max_age=timedelta(days=1)).fresh
    assert cache.load("daily","800001",now=AS_OF+timedelta(days=2),max_age=timedelta(days=1)).stale
    path.write_text("{bad",encoding="utf-8"); result=cache.load("daily","800001",now=AS_OF,max_age=timedelta(days=1))
    assert result.data is None and list(path.parent.glob("*.corrupt-*"))


def test_request_plan_skips_fresh_deduplicates_orders_limits_retry_and_resumes():
    states=[CacheState("800002","daily","stale"),CacheState("800001","flow","missing"),CacheState("800002","daily","stale"),CacheState("800001","master","missing"),CacheState("800003","daily","fresh"),CacheState("800004","daily","failed",2)]
    candidate=PreliminaryCandidate("800001",80,.9,True)
    full=build_request_plan(states,candidates=[candidate]); resumed=build_request_plan(states,candidates=[candidate],checkpoint=1)
    assert [(row.kind,row.code) for row in full.requests]==[("master","800001"),("master","800002"),("master","800003"),("master","800004"),("daily","800002"),("flow","800001")]
    assert full.skipped_fresh==1 and len(resumed.requests)==5


def test_local_master_operations_are_not_network_tr_requests():
    plan=build_request_plan([CacheState("800001","daily","missing")])
    assert plan.local_master_operations==1 and plan.network_tr_requests==1
    assert not plan.requests[0].network_tr


def test_flow_is_never_planned_before_weekly_hard_filter():
    candidates=[PreliminaryCandidate("800001",99,.9,False)]
    plan=build_request_plan([CacheState("800001","flow","missing")],candidates=candidates)
    assert plan.investor_flow_tr_requests==0


def test_default_flow_candidate_limit_is_120():
    candidates=[PreliminaryCandidate(f"{index:06d}",200-index,.9,True) for index in range(150)]
    assert len(select_flow_candidates(candidates,CollectionPolicy()))==120


def test_flow_plan_uses_actual_candidate_count_below_limit():
    candidates=[PreliminaryCandidate(f"{index:06d}",index,.9,True) for index in range(5)]
    plan=build_request_plan([],candidates=candidates,universe_codes=[item.code for item in candidates])
    assert plan.investor_flow_tr_requests==5


def test_flow_candidate_limit_is_configurable():
    candidates=[PreliminaryCandidate(f"{index:06d}",index,.9,True) for index in range(10)]
    plan=build_request_plan([],candidates=candidates,policy=CollectionPolicy(investor_candidate_limit=3))
    assert plan.investor_flow_tr_requests==3


def test_preliminary_tie_order_is_deterministic():
    candidates=[PreliminaryCandidate("800002",80,.9,True,trading_value=100),PreliminaryCandidate("800001",80,.9,True,trading_value=100)]
    assert [item.code for item in select_flow_candidates(candidates,CollectionPolicy())]==["800001","800002"]


def test_bootstrap_plans_missing_and_stale_daily_data():
    states=[CacheState("800001","daily","missing"),CacheState("800002","daily","stale")]
    assert build_request_plan(states,mode=CollectionMode.BOOTSTRAP).price_tr_requests==2


def test_daily_incremental_skips_fresh_and_updates_stale():
    states=[CacheState("800001","daily","fresh"),CacheState("800002","daily","stale")]
    plan=build_request_plan(states,mode=CollectionMode.DAILY_INCREMENTAL)
    assert plan.price_tr_requests==1 and plan.cached_requests_skipped==1


def test_repair_only_plans_failed_items():
    states=[CacheState("800001","daily","failed"),CacheState("800002","daily","stale")]
    plan=build_request_plan(states,mode=CollectionMode.REPAIR)
    assert plan.price_tr_requests==1 and any(item.code=="800001" and item.kind=="daily" for item in plan.requests)


def test_estimated_time_uses_network_only_and_scheduled_backoff():
    plan=build_request_plan([CacheState("800001","daily","missing")])
    plan.retry_requests=2
    assert plan.estimated_minimum_seconds==1+3+7


def test_market_program_flow_is_not_created_by_planner():
    candidate=PreliminaryCandidate("800001",80,.9,True)
    plan=build_request_plan([CacheState("800001","flow","missing")],candidates=[candidate])
    assert all(item.operation!="OPT90005" for item in plan.requests)


def test_partial_failure_does_not_stop_next_stock():
    output=[]; failures=[]
    for code in ("800001","800002"):
        try:
            if code=="800001": raise ValueError("fixture failure")
            output.append(code)
        except Exception as exc: failures.append((code,str(exc)))
    assert output==["800002"] and failures[0][0]=="800001"


def test_offline_validation_cli_never_starts_external_runtime(capsys):
    assert validate_recommendation_data_pipeline()["success"]
    def forbidden(*args,**kwargs): raise AssertionError("external call")
    assert run(["--validate-recommendation-data-pipeline"],application_factory=forbidden,adapter_factory=forbidden,lock_factory=forbidden,notification_service_factory=forbidden)==0
    text=capsys.readouterr().out
    assert "EXTERNAL_CALLS=0" in text and "VALIDATION: PASS" in text
    assert all(value not in text for value in ("None","null","unknown"))
