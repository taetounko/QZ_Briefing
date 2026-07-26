"""Cached-first validation of the full daily recommendation production path."""
from __future__ import annotations
import json
from datetime import date,datetime,time,timedelta
from pathlib import Path
from .daily_service import DailyRecommendationService,RecommendationReportStore
from .data_models import DataMetadata,DailyBar,PriceFeatures,RecommendationDataBundle,StockMasterRecord,InvestorFlowSnapshot
from .data_pipeline import weekly_ma5_metrics,aggregate_weekly_bars
from .scoring import evaluate_integrated_bundle
from qz_briefing.briefing.renderer import render_daily_recommendations
from qz_briefing.notifications.formatter import format_briefing,split_messages

def _payload(path:Path):
    value=json.loads(path.read_text(encoding="utf-8")); return value.get("data",value)
def _meta(x):
    return DataMetadata(str(x["code"]),str(x["name"]),str(x["market"]),datetime.fromisoformat(x["as_of"]),str(x["source"]),datetime.fromisoformat(x["updated_at"]),bool(x.get("complete",True)),bool(x.get("missing",False)),float(x.get("confidence",1)),x.get("collection_error"),bool(x.get("used_previous_trading_day",False)))
def load_cached_live_bundles(root:Path,max_symbols:int)->tuple[list[RecommendationDataBundle],list[dict[str,object]]]:
    live=root/"live_collection"; masters=_payload(live/"master"/"universe.json"); bundles=[]; diagnostics=[]
    for raw_master in masters[:max_symbols]:
        code=str(raw_master["metadata"]["code"]); daily_path=live/"daily"/f"{code}.json"; weekly_path=live/"weekly"/f"{code}.json"; feature_path=live/"features"/f"{code}.json"
        missing=[name for name,path in (("OPT10081",daily_path),("weekly",weekly_path),("features",feature_path)) if not path.exists()]
        if missing: diagnostics.append({"code":code,"status":"incomplete","missing":missing}); continue
        master=StockMasterRecord(_meta(raw_master["metadata"]),str(raw_master["security_type"]),bool(raw_master.get("tradable",True)),str(raw_master.get("trading_status","normal")),tuple(raw_master.get("risk_labels",[])),date.fromisoformat(raw_master["listed_date"]) if raw_master.get("listed_date") else None,raw_master.get("reference_price"),str(raw_master.get("raw_state","")))
        daily=[]
        for x in _payload(daily_path): daily.append(DailyBar(_meta(x["metadata"]),date.fromisoformat(x["trading_date"]),float(x["open"]),float(x["high"]),float(x["low"]),float(x["close"]),float(x["volume"]),float(x["trading_value"]) if x.get("trading_value") is not None else None,bool(x["adjusted"])))
        evaluation_as_of=datetime.combine(datetime.now().date(),time(16,0))
        weekly=list(aggregate_weekly_bars(tuple(daily),evaluation_as_of))
        f=_payload(feature_path); features=PriceFeatures(dict(f["values"]),tuple(f.get("missing",[])),float(f.get("confidence",1)))
        flow_diag=live/"diagnostics"/f"opt10059_{code}.json"; flow_info=_payload(flow_diag) if flow_diag.exists() else None
        raw_flow_path=live/"flow_raw"/f"{code}.json"; investor=None; flow_source="missing"
        if raw_flow_path.exists():
            raw_flow=_payload(raw_flow_path); rows=raw_flow.get("rows",[])
            foreign=tuple(float(row["foreign"]) for row in rows if row.get("foreign") is not None and row.get("institution") is not None)
            institution=tuple(float(row["institution"]) for row in rows if row.get("foreign") is not None and row.get("institution") is not None)
            flow_meta=DataMetadata(code,master.metadata.name,master.metadata.market,evaluation_as_of,"Kiwoom OPT10059 raw normalized rows",evaluation_as_of,True,len(rows)<20,1.0 if len(rows)>=20 else .7)
            investor=InvestorFlowSnapshot(flow_meta,foreign,institution); flow_source="raw_rows"
        elif flow_info: flow_source="diagnostic_summary_only"
        bundles.append(RecommendationDataBundle(master,tuple(daily),tuple(weekly),features,investor))
        completed=[x for x in weekly if x.metadata.complete]
        metrics=weekly_ma5_metrics(tuple(weekly)) or {}
        diagnostics.append({"code":code,"name":master.metadata.name,"market":master.metadata.market,"status":"ready","daily_count":len(daily),"first_date":daily[0].trading_date.isoformat(),"last_date":daily[-1].trading_date.isoformat(),"completed_weekly_count":len(completed),"last_completed_week":completed[-1].week_end.isoformat() if completed else "","weekly_close":metrics.get("weekly_close"),"weekly_ma5":metrics.get("weekly_ma5"),"weekly_above_ma5":metrics.get("weekly_close_above_ma5"),"weekly_distance":metrics.get("distance_rate"),"weekly_consecutive":metrics.get("consecutive_weeks"),"weekly_slope":metrics.get("ma5_slope"),"flow_cache":flow_source,"flow_row_count":len(investor.foreign_daily) if investor else 0,"flow_metrics":flow_info.get("metrics") if flow_info else None})
    return bundles,diagnostics

def run_cached_live_daily_validation(project_root:Path,max_symbols:int)->dict[str,object]:
    if not 1<=max_symbols<=5: raise ValueError("--max-symbols must be between 1 and 5")
    validation_root=project_root/"data"/"validation"/"recommendations"; bundles,diagnostics=load_cached_live_bundles(validation_root,max_symbols)
    if len(bundles)<max_symbols:return {"success":False,"status":"LIVE_DATA_INCOMPLETE","diagnostics":diagnostics,"external_calls":0,"cache_hits":len(bundles),"cache_misses":max_symbols-len(bundles)}
    trading_date=max(bar.trading_date for bundle in bundles for bar in bundle.daily_bars)
    generation_time=datetime.combine(trading_date,time(15,40))
    service=DailyRecommendationService(RecommendationReportStore(validation_root),lambda day:bundles,clock=lambda:generation_time,market_is_open=lambda day:True)
    generated=service.generate_market_close(trading_date); duplicate=service.generate_market_close(trading_date)
    report=generated.report or duplicate.report or {}; markdown="\n".join(render_daily_recommendations(report)); telegram=format_briefing({"briefing_type":"market_close","status":"completed","analysis":{},"daily_recommendations":report})
    groups={row["code"]:key for key in ("strong","review") for row in report.get(key,[])}
    for bundle,row in zip(bundles,[x for x in diagnostics if x.get("status")=="ready"]):
        score=evaluate_integrated_bundle(bundle); row.update({"total_score":score.total_score,"components":score.components,"risk_penalty":score.risk_deduction,"confidence":score.confidence,"missing":score.missing,"final_group":groups.get(score.item.code,"excluded" if not score.eligible else "not_selected"),"exclusion":"; ".join(score.exclusion_reasons)})
    safe=all(x not in markdown+telegram for x in ("None","null","unknown",str(project_root)))
    return {"success":generated.status in {"generated","reused"} and duplicate.status=="reused" and safe,"status":"PASS" if safe else "FAIL","diagnostics":diagnostics,"generation":generated,"duplicate":duplicate.status,"pre_market":service.load_pre_market(trading_date+timedelta(days=1)) is not None,"intraday":service.load_intraday(trading_date) is not None,"telegram_chunks":len(split_messages(telegram)),"telegram_sends":0,"order_account_tr":0,"external_calls":0,"cache_hits":len(bundles),"cache_misses":len([x for x in diagnostics if x.get('status')!='ready']),"report":report}

def print_cached_live_daily_validation(result:dict[str,object])->None:
    def safe(value:object)->str:return str(value).encode("cp949",errors="replace").decode("cp949")
    print(f"STATUS={result['status']}")
    for row in result.get("diagnostics",[]):
        print("SYMBOL="+" ".join(f"{k}={safe(v)}" for k,v in row.items() if k not in {"flow_metrics","components","missing"}))
        if row.get("flow_metrics"): print("  FLOW="+safe(row["flow_metrics"]))
        if row.get("components") is not None: print("  SCORE="+safe(row["components"])+f" missing={safe(row.get('missing',[]))}")
    report=result.get("report",{}); print(f"INPUT={report.get('input_count',0)} HARD_FILTER_PASS={report.get('hard_filter_pass_count',0)} STRONG={report.get('strong_count',0)} REVIEW={report.get('review_count',0)}")
    for group in ("strong","review"):
        for row in report.get(group,[]): print(safe(f"SCORE code={row['code']} group={group} total={row['total_score']} components={row['components']} risk={row['risk_penalty']} missing={row['missing']}"))
    print(f"DUPLICATE={result.get('duplicate','')} PREMARKET={result.get('pre_market',False)} INTRADAY={result.get('intraday',False)} TELEGRAM_CHUNKS={result.get('telegram_chunks',0)}")
    print(f"CACHE_HITS={result.get('cache_hits',0)} CACHE_MISSES={result.get('cache_misses',0)} LIVE_MASTER_CALLS={result.get('live_master_calls',0)}")
    print(f"OPT10081_REQUESTS={result.get('opt10081_requests',0)} OPT10081_SUCCESSES={result.get('opt10081_successes',0)} OPT10081_FAILURES={result.get('opt10081_failures',0)}")
    print(f"OPT10059_REQUESTS={result.get('opt10059_requests',0)} OPT10059_SUCCESSES={result.get('opt10059_successes',0)} OPT10059_FAILURES={result.get('opt10059_failures',0)}")
    print(f"LIVE_TR_CALLS={result.get('live_tr_calls',0)} EXTERNAL_CALLS={result.get('external_calls',0)} ORDER_ACCOUNT_TR={result.get('order_account_tr',0)} TELEGRAM_SENDS={result.get('telegram_sends',0)}")
    print("UI_MODE=headless_validation\nDASHBOARD_STARTED=0")
    print(f"LIVE DAILY RECOMMENDATION VALIDATION: {'PASS' if result.get('success') else result['status']}")
