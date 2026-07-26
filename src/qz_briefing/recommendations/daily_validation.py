"""Offline validation of the idempotent daily recommendation service."""
from __future__ import annotations
import tempfile
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from .daily_service import DailyRecommendationService, RecommendationReportStore
from .integrated_validation import validation_bundles

DAY=date(2026,7,24); NOW=datetime(2026,7,24,15,40)

def validate_daily_recommendation_service()->dict[str,object]:
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw); state={"bundles":validation_bundles()}; calls={"external":0}
        service=DailyRecommendationService(RecommendationReportStore(root/"validation"),lambda day:state["bundles"],clock=lambda:NOW,market_is_open=lambda day:True)
        first=service.generate_market_close(DAY); duplicate=service.generate_market_close(DAY)
        changed=list(state["bundles"]); changed[0]=replace(changed[0],master=replace(changed[0].master,raw_state="fixture-version-2")); state["bundles"]=changed
        regenerated=service.generate_market_close(DAY)
        restored=DailyRecommendationService(RecommendationReportStore(root/"validation"),lambda day:[],clock=lambda:NOW).load_intraday(DAY)
        premarket=service.load_pre_market(date(2026,7,27))
        closed=DailyRecommendationService(RecommendationReportStore(root/"closed"),lambda day:state["bundles"],clock=lambda:NOW,market_is_open=lambda day:False).generate_market_close(DAY)
        empty=DailyRecommendationService(RecommendationReportStore(root/"empty"),lambda day:[],clock=lambda:NOW).generate_market_close(DAY)
        checks={
            "market_close_generated":first.status=="generated" and len(first.paths)==3,
            "duplicate_reused":duplicate.status=="reused" and duplicate.content_hash==first.content_hash,
            "changed_input_regenerated":regenerated.status=="generated" and regenerated.content_hash!=first.content_hash,
            "restart_restored":restored is not None and restored.get("content_hash")==regenerated.content_hash,
            "pre_market_previous_loaded":premarket is not None,
            "market_closed_blocked":closed.status=="market_closed",
            "missing_input_nonfatal":empty.status=="input_unavailable",
            "atomic_no_tmp":not list(root.rglob("*.tmp")),
            "version_history":len(list((root/"validation"/"reports"/DAY.isoformat()/"versions").iterdir()))==2,
            "validation_separated":not (root/"recommendations").exists(),
            "telegram_registered_after_save":first.telegram_registration_count==1 and duplicate.telegram_registration_count==0,
            "external_calls":calls["external"]==0,
        }
        return {"success":all(checks.values()),"checks":checks,"result":regenerated,"pre_market_loaded":premarket is not None,"intraday_loaded":restored is not None,"external_calls":0}

def print_daily_recommendation_validation(result:dict[str,object])->None:
    generated=result["result"]; report=generated.report or {}
    print(f"TRADING_DATE={report.get('trading_date','자료 부족')} INPUT={report.get('input_count',0)} HARD_FILTER_PASS={report.get('hard_filter_pass_count',0)} EVALUABLE={report.get('evaluable_count',0)}")
    print(f"STRONG={report.get('strong_count',0)} REVIEW={report.get('review_count',0)} HASH={generated.content_hash}")
    print(f"FILES={len(generated.paths)} PREMARKET_LOADED={result['pre_market_loaded']} INTRADAY_LOADED={result['intraday_loaded']} TELEGRAM_REGISTRATIONS={generated.telegram_registration_count}")
    for name,passed in result["checks"].items():print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print(f"EXTERNAL_CALLS={result['external_calls']}")
    print(f"DAILY RECOMMENDATION SERVICE VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
