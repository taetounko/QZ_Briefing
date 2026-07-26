from datetime import date,datetime,time,timedelta
from pathlib import Path
import shutil

from qz_briefing.__main__ import run
from qz_briefing.notifications.formatter import format_briefing,split_messages
from qz_briefing.recommendations.daily_service import DailyRecommendationService,RecommendationReportStore
from qz_briefing.recommendations.live_daily_validation import load_cached_live_bundles,run_cached_live_daily_validation
from qz_briefing.recommendations.scoring import evaluate_integrated_bundle
from qz_briefing.recommendations.data_models import DataMetadata,DailyBar
from qz_briefing.recommendations.data_pipeline import aggregate_weekly_bars
import pytest

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/"data"/"validation"/"recommendations"

def test_cached_loader_uses_validation_data_and_limits_five():
    bundles,diagnostics=load_cached_live_bundles(CACHE,5)
    assert len(bundles)==5 and len(diagnostics)==5
    assert all(len(bundle.daily_bars)==260 for bundle in bundles)

def test_cached_daily_rows_have_no_duplicates_future_or_invalid_ohlc():
    bundles,_=load_cached_live_bundles(CACHE,5)
    for bundle in bundles:
        dates=[bar.trading_date for bar in bundle.daily_bars]
        assert len(dates)==len(set(dates)) and max(dates)<=bundle.master.metadata.as_of.date()
        assert all(bar.low<=min(bar.open,bar.close)<=max(bar.open,bar.close)<=bar.high and bar.volume>=0 for bar in bundle.daily_bars)

def test_cached_weekly_data_excludes_incomplete_week_from_hard_filter():
    bundles,_=load_cached_live_bundles(CACHE,5)
    assert all(len([bar for bar in bundle.weekly_bars if bar.metadata.complete])>=5 for bundle in bundles)
    assert all([bar for bar in bundle.weekly_bars if bar.metadata.complete][-1].week_end==date(2026,7,24) for bundle in bundles)

def _cache_without_raw_flow(tmp_path:Path)->Path:
    isolated=tmp_path/"recommendations"
    shutil.copytree(CACHE,isolated)
    shutil.rmtree(isolated/"live_collection"/"flow_raw",ignore_errors=True)
    return isolated

def test_opt10059_summary_is_not_fabricated_into_daily_flow(tmp_path):
    bundles,diagnostics=load_cached_live_bundles(_cache_without_raw_flow(tmp_path),5)
    assert all(bundle.investor_flow is None for bundle in bundles)
    assert any(row.get("flow_cache")=="diagnostic_summary_only" for row in diagnostics)

def test_missing_flow_and_catalyst_remain_unavailable_with_real_prices(tmp_path):
    bundles,_=load_cached_live_bundles(_cache_without_raw_flow(tmp_path),5)
    score=next(
        value for value in map(evaluate_integrated_bundle,bundles)
        if value.eligible and value.features.fund_flow_status=="data_unavailable"
    )
    assert score.components["fund_inflow"]==0 and score.components["catalyst"]==0
    assert score.preliminary.evaluation_status=="partial"

def test_cached_validation_saves_only_validation_report_and_reuses_hash(tmp_path):
    bundles,_=load_cached_live_bundles(CACHE,2); as_of=max(x.master.metadata.as_of for x in bundles).replace(hour=15,minute=40)
    service=DailyRecommendationService(RecommendationReportStore(tmp_path/"validation"),lambda day:bundles,clock=lambda:as_of)
    first=service.generate_market_close(as_of.date()); second=service.generate_market_close(as_of.date())
    assert first.status=="generated" and second.status=="reused" and first.content_hash==second.content_hash
    assert not (tmp_path/"operational").exists()

def test_cached_end_to_end_has_zero_external_calls_and_restores_briefings():
    result=run_cached_live_daily_validation(ROOT,5)
    assert result["success"] and result["external_calls"]==result["telegram_sends"]==result["order_account_tr"]==0
    assert result["pre_market"] and result["intraday"] and result["duplicate"]=="reused"

def test_live_cli_rejects_invalid_limits_without_external_calls(capsys):
    assert run(["--validate-live-daily-recommendation","--max-symbols","6","--cached-only"])==2
    assert "must be 1..5" in capsys.readouterr().out

def test_live_cli_requires_explicit_mode(capsys):
    assert run(["--validate-live-daily-recommendation","--max-symbols","1"])==2
    assert "--cached-only or --allow-kiwoom-live" in capsys.readouterr().out

def test_telegram_is_preview_only_and_safely_split():
    result=run_cached_live_daily_validation(ROOT,5); report=result["report"]
    text=format_briefing({"briefing_type":"market_close","status":"completed","analysis":{},"daily_recommendations":report})
    assert len(split_messages(text,200))>=1 and result["telegram_sends"]==0
    assert all(token not in text for token in ("None","null","unknown",str(ROOT)))

def _week(last_day:date,as_of:datetime,expected:date|None=None):
    meta=DataMetadata("990001","가상주봉","KOSPI",as_of,"fixture",as_of)
    monday=last_day-timedelta(days=last_day.weekday()); rows=[]
    day=monday
    while day<=last_day:
        if day.weekday()<5: rows.append(DailyBar(meta,day,100,102,99,101,1000,1000,True))
        day+=timedelta(days=1)
    mapping={(last_day.isocalendar().year,last_day.isocalendar().week):expected} if expected else None
    return aggregate_weekly_bars(tuple(rows),as_of,week_last_trading_days=mapping)[-1]

@pytest.mark.parametrize(("last_day","as_of","complete"),[
    (date(2026,7,20),datetime(2026,7,20,12),False),
    (date(2026,7,23),datetime(2026,7,23,16),False),
    (date(2026,7,24),datetime(2026,7,24,15,39),False),
    (date(2026,7,24),datetime(2026,7,24,15,40),True),
    (date(2026,7,24),datetime(2026,7,25,10),True),
    (date(2026,7,24),datetime(2026,7,26,10),True),
    (date(2026,7,24),datetime(2026,7,27,8),True),
    (date(2026,7,22),datetime(2026,7,26,10),False),
])
def test_week_completion_policy(last_day,as_of,complete):
    result=_week(last_day,as_of)
    assert result.metadata.complete is complete and result.week_end==last_day

def test_short_holiday_week_requires_explicit_last_session():
    last=date(2026,7,23)
    assert not _week(last,datetime(2026,7,23,16)).metadata.complete
    assert _week(last,datetime(2026,7,23,16),last).metadata.complete

def test_force_refresh_requires_allow_live(capsys):
    assert run(["--validate-live-daily-recommendation","--max-symbols","1","--force-kiwoom-refresh"])==2
    assert "requires --allow-kiwoom-live" in capsys.readouterr().out
