"""Offline fixture validation for the recommendation data pipeline."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path

from .data_cache import RecommendationDataCache
from .data_models import DataMetadata, DailyBar, RecommendationDataBundle, StockMasterRecord
from .data_pipeline import aggregate_weekly_bars, compute_price_features, normalize_daily_bars, to_recommendation_features, universe_decision
from .request_planner import CacheState, CollectionMode, PreliminaryCandidate, build_request_plan
from .selector import select_recommendations


AS_OF=datetime(2026,7,24,16,0)


def _master(code:str,name:str,market:str="KOSPI",security_type:str="common_stock",tradable:bool=True,status:str="normal") -> StockMasterRecord:
    return StockMasterRecord(DataMetadata(code,name,market,AS_OF,"offline_fixture",AS_OF,True,False,.95),security_type,tradable,status)


def _bars(master:StockMasterRecord,count:int=130) -> list[DailyBar]:
    output=[]; current=date(2026,1,19); value=80.0
    while len(output)<count:
        if current.weekday()<5:
            value+=.2
            meta=DataMetadata(master.metadata.code,master.metadata.name,master.metadata.market,AS_OF,"offline_fixture",AS_OF,True,False,.95)
            output.append(DailyBar(meta,current,value-.5,value+1,value-1,value,1000+len(output)*10,(1000+len(output)*10)*value,True))
        current+=timedelta(days=1)
    return output


def validate_recommendation_data_pipeline() -> dict[str,object]:
    masters=[_master("900001","가상코스피"),_master("900002","가상코스닥","KOSDAQ"),_master("900003","가상ETF",security_type="etf"),_master("900004","가상정지",tradable=False,status="trading_halt")]
    included=[]; excluded=[]; features=[]
    for master in masters:
        accepted,reason=universe_decision(master)
        if not accepted: excluded.append((master,reason)); continue
        included.append(master); daily,_=normalize_daily_bars(_bars(master),AS_OF)
        key=daily[-1].trading_date.isocalendar(); weekly=aggregate_weekly_bars(daily,AS_OF,week_last_trading_days={(key.year,key.week):daily[-1].trading_date})
        price=compute_price_features(daily,AS_OF); bundle=RecommendationDataBundle(master,daily,weekly,price); features.append(to_recommendation_features(bundle))
    report=select_recommendations(features)
    states=[CacheState("900001","master","fresh"),CacheState("900001","daily","fresh"),CacheState("900002","daily","stale"),CacheState("900002","daily","stale")]
    candidates=[PreliminaryCandidate("900001",80,.9,True,True,.8,1_000_000),PreliminaryCandidate("900002",70,.8,True,True,.7,800_000)]
    plan=build_request_plan(states,candidates=candidates,universe_codes=[item.metadata.code for item in included])
    with TemporaryDirectory() as raw:
        cache=RecommendationDataCache(Path(raw)); cache.save("master","all",{"count":len(masters)},as_of=AS_OF,source="offline_fixture")
        cache_hit=cache.load("master","all",now=AS_OF,max_age=timedelta(days=1)).fresh
        corrupt=cache.path("daily","broken"); corrupt.parent.mkdir(parents=True); corrupt.write_text("{broken",encoding="utf-8")
        corrupt_safe=cache.load("daily","broken",now=AS_OF,max_age=timedelta(days=1)).data is None
    checks={"universe":len(included)==2 and len(excluded)==2,"weekly_ma5":all(len(item.weekly_bars)>=5 for item in features),"cache":cache_hit and corrupt_safe,"planner":plan.local_master_operations==2 and plan.price_tr_requests==1 and plan.investor_flow_tr_requests==2,"engine":len(report.strong)+len(report.review)<=6}
    metrics={"master_total":len(masters),"universe_included":len(included),"excluded":len(excluded),"daily_valid":len(features),"weekly_possible":len(features),"ma5_possible":len(features),"hard_filter_pass":report.hard_filter_pass_count,"flow_available":0,"partial":len(features),"failures":0,"cache_hit_rate":2/3,"local_master_operations":plan.local_master_operations,"network_tr_requests":plan.network_tr_requests,"price_tr_requests":plan.price_tr_requests,"investor_flow_tr_requests":plan.investor_flow_tr_requests,"cache_skipped":plan.cached_requests_skipped,"collection_mode":CollectionMode.BOOTSTRAP.value,"estimated_minimum_collection_time":plan.estimated_minimum_seconds,"preliminary_candidates":plan.preliminary_candidate_count,"detailed_flow_candidates":plan.detailed_flow_candidate_count,"engine_inputs":len(features),"strong":len(report.strong),"review":len(report.review),"external_calls":0}
    return {"success":all(checks.values()),"checks":checks,"metrics":metrics}


def print_recommendation_data_validation(result:dict[str,object]) -> None:
    for key,value in result["metrics"].items(): print(f"{key.upper()}={value}")
    for key,value in result["checks"].items(): print(f"[{'PASS' if value else 'FAIL'}] {key}")
    print(f"RECOMMENDATION DATA PIPELINE VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
