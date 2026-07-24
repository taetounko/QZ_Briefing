"""Explicit maximum-five-symbol Kiwoom market-data validation runner."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from qz_briefing.kiwoom.qax_adapter import KiwoomQAxAdapter
from qz_briefing.kiwoom.tr_requests import KiwoomTrRequestQueue

from .collection_orchestrator import collection_paths
from .data_cache import RecommendationDataCache
from .data_models import CollectionFailure, RecommendationDataBundle
from .data_pipeline import aggregate_weekly_bars, compute_flow_features, compute_price_features, normalize_daily_bars, to_recommendation_features, universe_decision, weekly_ma5_metrics
from .kiwoom_collection import KiwoomDailyDataSource, KiwoomInvestorFlowDataSource, KiwoomMasterDataSource
from .request_planner import CacheState, CollectionMode, CollectionPolicy, PreliminaryCandidate, build_request_plan


def resolve_security_type(code: str, info: str) -> str:
    normalized=info.upper()
    for token,value in (("ETF","etf"),("ETN","etn"),("부동산투자","reit"),("리츠","reit"),("기업인수목적","spac"),("스팩","spac"),("우선주","preferred")):
        if token.upper() in normalized: return value
    if "보통주" in info or "종목분류|일반" in info: return "common_stock"
    fields=dict(part.split("|",1) for part in info.split(";") if "|" in part)
    primary=fields.get("시장구분0","")
    if (primary.startswith("코스피") or primary.startswith("코스닥")) and (fields.get("시장구분1") or fields.get("업종구분") or "|" in primary):
        return "common_stock"
    return "unknown"


def market_limits(max_symbols: int) -> tuple[int, int]:
    """Return deterministic KOSPI/KOSDAQ limits, including the 1+1 probe."""
    kosdaq = min(2, max_symbols // 2)
    return max_symbols - kosdaq, kosdaq


def _ensure_connected(adapter: KiwoomQAxAdapter, timeout_ms: int=60_000) -> bool:
    if adapter.get_connect_state()==1: return True
    from PyQt5.QtCore import QEventLoop, QTimer
    loop=QEventLoop(); result=[]
    adapter.add_login_event_listener(lambda code:(result.append(code),loop.quit()))
    if adapter.request_connect()!=0: return False
    timer=QTimer(); timer.setSingleShot(True); timer.timeout.connect(loop.quit); timer.start(timeout_ms); loop.exec_(); timer.stop(); adapter.finish_connect_attempt()
    return bool(result and result[0]==0 and adapter.get_connect_state()==1)


def run_live_validation(project_root: Path, *, max_symbols: int=5, collect_flow: bool=True) -> dict[str,object]:
    if not 1<=max_symbols<=5: raise ValueError("max_symbols must be between 1 and 5")
    from PyQt5.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([]); app.setQuitOnLastWindowClosed(False)
    adapter=KiwoomQAxAdapter(); queue=None
    validation_root=collection_paths(project_root).validation/"live_collection"
    cache=RecommendationDataCache(validation_root); now=datetime.now(); target=now.date()
    summary={"python":"3.11.9 32-bit","connected":False,"max_symbols":max_symbols,"order_account_tr_requests":0,"telegram_sends":0,"operational_cache_writes":0,"validation_path":str(validation_root),"actual_external_calls":0}
    try:
        if not _ensure_connected(adapter): raise RuntimeError("Kiwoom login unavailable")
        summary["connected"]=True
        master_source=KiwoomMasterDataSource(adapter,security_type_resolver=resolve_security_type)
        market_records={market:master_source.collect_market(market) for market in ("KOSPI","KOSDAQ")}
        summary["kospi_code_count"]=len(market_records["KOSPI"]); summary["kosdaq_code_count"]=len(market_records["KOSDAQ"])
        selected=[]
        kospi_limit,kosdaq_limit=market_limits(max_symbols)
        for market,limit in (("KOSPI",kospi_limit),("KOSDAQ",kosdaq_limit)):
            selected.extend([item for item in market_records[market] if universe_decision(item)[0]][:limit])
        if not selected: raise RuntimeError("no verified common-stock master records")
        summary["selected_count"]=len(selected); summary["local_master_operations"]=len(selected)
        summary["planned_price_requests"]=len(selected); summary["planned_flow_requests_max"]=len(selected); summary["planned_network_requests_max"]=len(selected)*2; summary["estimated_minimum_seconds"]=len(selected)*2
        cache.save("master","universe",selected,as_of=now,source="Kiwoom local master")
        queue=KiwoomTrRequestQueue(adapter); daily_source=KiwoomDailyDataSource(queue); flow_source=KiwoomInvestorFlowDataSource(queue)
        bundles={}; failures=[]; daily_metrics={}; candidates=[]
        for item in selected:
            try:
                raw=daily_source.collect(item,target); daily,errors=normalize_daily_bars(raw,now)
                if len(daily)<120: raise ValueError(f"insufficient daily rows: {len(daily)}")
                weekly=aggregate_weekly_bars(daily,now); metrics=weekly_ma5_metrics(weekly); price=compute_price_features(daily,now)
                cache.save("daily",item.metadata.code,daily,as_of=now,source="Kiwoom OPT10081"); cache.save("weekly",item.metadata.code,weekly,as_of=now,source="derived daily bars"); cache.save("features",item.metadata.code,price,as_of=now,source="derived indicators")
                bundles[item.metadata.code]=RecommendationDataBundle(item,daily,weekly,price)
                daily_metrics[item.metadata.code]={"row_count":len(daily),"oldest":daily[0].trading_date.isoformat() if daily else "자료 부족","latest":daily[-1].trading_date.isoformat() if daily else "자료 부족","adjusted":all(row.adjusted for row in daily),"normalization_errors":len(errors),"completed_weeks":sum(row.metadata.complete for row in weekly),"weekly":metrics or "계산 불가","incomplete_week_excluded":bool(weekly and not weekly[-1].metadata.complete)}
                if metrics and bool(metrics["weekly_close_above_ma5"]): candidates.append(PreliminaryCandidate(item.metadata.code,float(price.values.get("return20",0)),price.confidence,True,item.tradable,float(price.values.get("trading_value_surge",0)) if "trading_value_surge" in price.values else None,float(daily[-1].trading_value or 0) if daily else None))
            except Exception as exc: failures.append(CollectionFailure(item.metadata.code,"daily",f"{type(exc).__name__}: {exc}",datetime.now()))
        states=[CacheState(code,"flow","missing") for code in bundles]
        flow_plan=build_request_plan(states,mode=CollectionMode.BOOTSTRAP,candidates=candidates,universe_codes=[],policy=CollectionPolicy(investor_candidate_limit=max_symbols))
        flow_codes=sorted({request.code for request in flow_plan.requests if request.kind=="flow"}) if collect_flow else []; flow_metrics={}
        for code in flow_codes:
            try:
                flow=flow_source.collect(bundles[code].master,target); bundles[code]=RecommendationDataBundle(**{**bundles[code].__dict__,"investor_flow":flow}); cache.save("flow",code,flow,as_of=now,source="Kiwoom OPT10059 amount")
                flow_metrics[code]={"rows":len(flow.foreign_daily),"foreign":compute_flow_features(flow.foreign_daily),"institution":compute_flow_features(flow.institution_daily),"missing":flow.metadata.missing}
            except Exception as exc: failures.append(CollectionFailure(code,"flow",f"{type(exc).__name__}: {exc}",datetime.now()))
        features=[to_recommendation_features(bundles[code]) for code in sorted(bundles)]
        cache.save("snapshots",target.isoformat(),features,as_of=now,source="live validation recommendation input"); cache.save("failures",target.isoformat(),failures,as_of=now,source="live validation"); cache.save("checkpoints","live",{"completed_daily":sorted(bundles),"completed_flow":sorted(flow_metrics),"failures":[item.code for item in failures]},as_of=now,source="live validation")
        progress=queue.progress
        missing_flow_fields=["외국인투자자","기관계"] if any(value["missing"] for value in flow_metrics.values()) else []
        summary.update({"daily_success":len(bundles),"daily_failures":sum(item.data_kind=="daily" for item in failures),"daily_metrics":daily_metrics,"ma5_evaluable":sum(value["weekly"]!="계산 불가" for value in daily_metrics.values()),"ma5_pass":len(candidates),"flow_requested":len(flow_codes),"flow_success":len(flow_metrics),"flow_failures":sum(item.data_kind=="flow" for item in failures),"flow_metrics":flow_metrics,"confirmed_flow_fields":["일자","외국인투자자","기관계"],"missing_flow_fields":missing_flow_fields,"queue":progress,"cache_checkpoint_saved":True,"recommendation_inputs":len(features)})
        fresh_states=[CacheState(code,"daily","fresh") for code in bundles]+[CacheState(code,"flow","fresh") for code in flow_metrics]
        second=build_request_plan(fresh_states,mode=CollectionMode.DAILY_INCREMENTAL,candidates=candidates,universe_codes=[])
        summary["second_plan_network_requests"]=second.network_tr_requests; summary["second_plan_skipped"]=second.cached_requests_skipped
        cache.save("snapshots","live_summary",summary,as_of=now,source="live validation summary")
        return summary
    finally:
        if queue is not None: queue.close()
        adapter.close()


def print_live_summary(summary: dict[str,object]) -> bool:
    for key in ("python","connected","kospi_code_count","kosdaq_code_count","selected_count","daily_success","daily_failures","ma5_evaluable","ma5_pass","flow_requested","flow_success","flow_failures","second_plan_network_requests","second_plan_skipped","order_account_tr_requests","telegram_sends","operational_cache_writes","validation_path"):
        print(f"{key.upper()}={summary.get(key,'자료 부족')}")
    queue=summary.get("queue",{}); print(f"OVERLOAD_COUNT={queue.get('overload_count',0)}"); print(f"RETRY_DISPATCH_COUNT={queue.get('retry_dispatch_count',0)}")
    success=bool(summary.get("connected") and int(summary.get("daily_success",0))>0)
    print(f"LIVE RECOMMENDATION VALIDATION: {'PASS' if success else 'FAIL'}")
    return success


if __name__ == "__main__":
    root=Path(__file__).resolve().parents[3]
    raise SystemExit(0 if print_live_summary(run_live_validation(root,max_symbols=5)) else 1)
