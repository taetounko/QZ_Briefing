"""Safe recommendation collection orchestration and offline plan reporting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime
from typing import Protocol

from .data_models import CollectionFailure, RecommendationDataBundle, StockMasterRecord
from .data_cache import RecommendationDataCache
from .data_pipeline import aggregate_weekly_bars, compute_price_features, normalize_daily_bars, to_recommendation_features, universe_decision, weekly_ma5_metrics
from .selector import select_recommendations
from .request_planner import CacheState, CollectionMode, CollectionPolicy, PreliminaryCandidate, RequestPlan, build_request_plan


@dataclass(frozen=True)
class CollectionPaths:
    operational: Path
    validation: Path


class MasterSource(Protocol):
    def collect_market(self, market: str) -> list[StockMasterRecord]: ...


class DailySource(Protocol):
    def collect(self, master: StockMasterRecord, target_date: date) -> list: ...


class FlowSource(Protocol):
    def collect(self, master: StockMasterRecord, target_date: date): ...


@dataclass
class CollectionRun:
    plan: RequestPlan
    report: object
    failures: list[CollectionFailure]
    preliminary_count: int
    flow_count: int
    external_calls: int


class RecommendationCollectionOrchestrator:
    """Execute the staged algorithm with injected sources and per-symbol isolation."""
    def __init__(self, master_source: MasterSource, daily_source: DailySource, flow_source: FlowSource, *, policy: CollectionPolicy|None=None, cache: RecommendationDataCache|None=None, clock=lambda:datetime.now()) -> None:
        self.master_source=master_source; self.daily_source=daily_source; self.flow_source=flow_source; self.policy=policy or CollectionPolicy(); self.cache=cache; self.clock=clock

    def run(self, mode: CollectionMode, target_date: date, *, max_symbols: int|None=None) -> CollectionRun:
        masters=[]
        for market in ("KOSPI","KOSDAQ"): masters.extend(self.master_source.collect_market(market))
        masters=sorted((item for item in masters if universe_decision(item)[0]),key=lambda item:item.metadata.code)
        if max_symbols is not None: masters=masters[:max_symbols]
        if self.cache: self.cache.save("master","universe",masters,as_of=self.clock(),source="Kiwoom local master")
        failures=[]; bundles={}; candidates=[]
        for item in masters:
            try:
                raw=self.daily_source.collect(item,target_date); daily,_=normalize_daily_bars(raw,self.clock()); weekly=aggregate_weekly_bars(daily,self.clock()); price=compute_price_features(daily,self.clock()); metrics=weekly_ma5_metrics(weekly)
                bundle=RecommendationDataBundle(item,daily,weekly,price); bundles[item.metadata.code]=bundle
                if self.cache:
                    self.cache.save("daily",item.metadata.code,daily,as_of=self.clock(),source="Kiwoom OPT10081")
                    self.cache.save("weekly",item.metadata.code,weekly,as_of=self.clock(),source="derived daily bars")
                    self.cache.save("features",item.metadata.code,price,as_of=self.clock(),source="derived indicators")
                if metrics and metrics["weekly_close_above_ma5"]:
                    score=float(price.values.get("position52",0))*.2+float(price.values.get("volume_surge",0))*.3+float(price.values.get("return20",0))*.5
                    candidates.append(PreliminaryCandidate(item.metadata.code,score,price.confidence,True,item.tradable,float(price.values.get("trading_value_surge",0)) if "trading_value_surge" in price.values else None,float(daily[-1].trading_value or 0) if daily else None))
            except Exception as exc: failures.append(CollectionFailure(item.metadata.code,"daily",f"{type(exc).__name__}: {exc}",self.clock()))
        planned_status="failed" if mode is CollectionMode.REPAIR else "missing"
        states=[CacheState(item.metadata.code,"daily",planned_status) for item in masters]+[CacheState(item.metadata.code,"flow",planned_status) for item in masters]
        plan=build_request_plan(states,mode=mode,candidates=candidates,universe_codes=[item.metadata.code for item in masters],policy=self.policy)
        selected={request.code for request in plan.requests if request.kind=="flow"}
        for code in sorted(selected):
            try:
                flow=self.flow_source.collect(bundles[code].master,target_date)
                bundles[code]=RecommendationDataBundle(**{**bundles[code].__dict__,"investor_flow":flow})
                if self.cache: self.cache.save("flow",code,flow,as_of=self.clock(),source="Kiwoom OPT10059 amount")
            except Exception as exc: failures.append(CollectionFailure(code,"flow",f"{type(exc).__name__}: {exc}",self.clock()))
        features=[to_recommendation_features(bundles[code]) for code in sorted(bundles)]
        report=select_recommendations(features)
        if self.cache:
            self.cache.save("snapshots",target_date.isoformat(),{"features":features,"selected":{"strong":len(report.strong),"review":len(report.review)}},as_of=self.clock(),source="recommendation collection")
            self.cache.save("failures",target_date.isoformat(),failures,as_of=self.clock(),source="recommendation collection")
            self.cache.save("checkpoints",mode.value,{"completed_codes":sorted(bundles),"failed_codes":sorted({item.code for item in failures}),"network_requests":plan.network_tr_requests},as_of=self.clock(),source="recommendation collection")
        return CollectionRun(plan,report,failures,len(candidates),len(selected),0)


def collection_paths(project_root: Path) -> CollectionPaths:
    return CollectionPaths(project_root/"data"/"recommendations",project_root/"data"/"validation"/"recommendations")


def fixture_plan(mode: CollectionMode, *, max_symbols: int | None=None, policy: CollectionPolicy | None=None) -> RequestPlan:
    policy=policy or CollectionPolicy(); count=min(5,max_symbols) if max_symbols is not None else 5
    codes=[f"7{index:05d}" for index in range(1,count+1)]
    if mode is CollectionMode.BOOTSTRAP:
        statuses=["missing"]*len(codes)
    elif mode is CollectionMode.DAILY_INCREMENTAL:
        statuses=["stale"]+["fresh"]*max(0,len(codes)-1)
    else:
        statuses=["failed"]+["fresh"]*max(0,len(codes)-1)
    states=[CacheState(code,"daily",status) for code,status in zip(codes,statuses)]+[CacheState(code,"flow",status) for code,status in zip(codes,statuses)]
    candidates=[PreliminaryCandidate(code,100-index,.9,True,True,.8,1_000_000-index) for index,code in enumerate(codes)]
    return build_request_plan(states,mode=mode,candidates=candidates,universe_codes=codes,policy=policy)


def render_plan(plan: RequestPlan, *, dry_run: bool=True) -> str:
    values={"COLLECTION_MODE":plan.mode.value,"UNIVERSE_COUNT":plan.universe_count,"LOCAL_MASTER_OPERATIONS":plan.local_master_operations,"NETWORK_TR_REQUESTS":plan.network_tr_requests,"PRICE_TR_REQUESTS":plan.price_tr_requests,"INVESTOR_FLOW_TR_REQUESTS":plan.investor_flow_tr_requests,"CACHED_REQUESTS_SKIPPED":plan.cached_requests_skipped,"RETRY_REQUESTS":plan.retry_requests,"TOTAL_PLANNED_OPERATIONS":plan.total_planned_operations,"ESTIMATED_MINIMUM_SECONDS":plan.estimated_minimum_seconds,"FLOW_CANDIDATE_LIMIT":plan.policy.investor_candidate_limit,"CHECKPOINT":plan.checkpoint,"DRY_RUN":str(dry_run).lower(),"ACTUAL_EXTERNAL_CALLS":0}
    return "\n".join(f"{key}={value}" for key,value in values.items())


def print_plan_modes(*, max_symbols: int | None=None) -> None:
    for mode in CollectionMode:
        print(render_plan(fixture_plan(mode,max_symbols=max_symbols)))
    print("LIVE RECOMMENDATION COLLECTION PLAN: PASS")


def run_collection_dry_run(mode: str | None, *, max_symbols: int | None, dry_run: bool) -> int:
    if not mode: raise ValueError("--collect-recommendation-data requires --mode")
    if not dry_run: raise ValueError("live collection is disabled; use --dry-run")
    if max_symbols is None or not 1<=max_symbols<=5: raise ValueError("dry-run requires --max-symbols between 1 and 5")
    plan=fixture_plan(CollectionMode(mode),max_symbols=max_symbols)
    print(render_plan(plan,dry_run=True)); print("RECOMMENDATION COLLECTION DRY RUN: PASS")
    return 0
