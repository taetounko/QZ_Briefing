from dataclasses import replace
from datetime import date, datetime

from qz_briefing.briefing.renderer import render_daily_recommendations
from qz_briefing.notifications.formatter import format_briefing
from qz_briefing.recommendations.daily_service import DailyRecommendationService, RecommendationReportStore
from qz_briefing.recommendations.integrated_validation import validation_bundles

DAY=date(2026,7,24); AFTER=datetime(2026,7,24,15,40)

def service(tmp_path,bundles=None,now=AFTER,opened=True,store=None):
    return DailyRecommendationService(store or RecommendationReportStore(tmp_path),lambda day:list(validation_bundles() if bundles is None else bundles),clock=lambda:now,market_is_open=lambda day:opened)

def test_market_close_generation_starts_at_1540(tmp_path):
    assert service(tmp_path,now=datetime(2026,7,24,15,39)).generate_market_close(DAY).status=="not_due"
    assert service(tmp_path).generate_market_close(DAY).status=="generated"

def test_same_input_is_reused_after_restart(tmp_path):
    first=service(tmp_path).generate_market_close(DAY); second=service(tmp_path).generate_market_close(DAY)
    assert first.content_hash==second.content_hash and second.status=="reused"

def test_changed_input_creates_version_history(tmp_path):
    bundles=validation_bundles(); first=service(tmp_path,bundles).generate_market_close(DAY)
    bundles[0]=replace(bundles[0],master=replace(bundles[0].master,raw_state="changed"))
    second=service(tmp_path,bundles).generate_market_close(DAY)
    assert first.content_hash!=second.content_hash and len(list((tmp_path/"reports"/DAY.isoformat()/"versions").iterdir()))==2

def test_json_markdown_metadata_are_atomic_and_restorable(tmp_path):
    result=service(tmp_path).generate_market_close(DAY)
    assert all(__import__('pathlib').Path(path).exists() for path in result.paths)
    assert not list(tmp_path.rglob("*.tmp")) and service(tmp_path,[]).load_intraday(DAY)["content_hash"]==result.content_hash

def test_pre_market_loads_latest_prior_trading_result(tmp_path):
    service(tmp_path).generate_market_close(DAY)
    assert service(tmp_path,[]).load_pre_market(date(2026,7,27))["trading_date"]==DAY.isoformat()

def test_market_closed_and_missing_input_are_nonfatal(tmp_path):
    assert service(tmp_path,opened=False).generate_market_close(DAY).status=="market_closed"
    assert service(tmp_path,bundles=[]).generate_market_close(DAY).status=="input_unavailable"

def test_zero_hard_filter_pass_is_valid_empty_report(tmp_path):
    bundles=[validation_bundles()[5]]
    result=service(tmp_path,bundles).generate_market_close(DAY)
    assert result.status=="generated" and result.report["hard_filter_pass_count"]==0
    assert result.report["strong_count"]==result.report["review_count"]==0

def test_no_forced_strong_candidates(tmp_path):
    result=service(tmp_path,validation_bundles()[8:10]).generate_market_close(DAY)
    assert result.report["strong_count"]==0

def test_validation_and_operational_roots_are_separate(tmp_path):
    operational=service(tmp_path/"operational").generate_market_close(DAY)
    validation=service(tmp_path/"validation").generate_market_close(DAY)
    assert operational.paths[0]!=validation.paths[0]

def test_storage_failure_prevents_telegram_registration(tmp_path):
    class BrokenStore(RecommendationReportStore):
        def save(self,*args,**kwargs): raise OSError("fixture failure")
    result=service(tmp_path,store=BrokenStore(tmp_path)).generate_market_close(DAY)
    assert result.status=="failed" and result.telegram_registration_count==0

def test_briefing_markdown_and_telegram_are_safe(tmp_path):
    report=service(tmp_path).generate_market_close(DAY).report
    markdown="\n".join(render_daily_recommendations(report))
    message=format_briefing({"briefing_type":"market_close","status":"completed","analysis":{},"daily_recommendations":report})
    assert "일일 추천 후보" in markdown and "[오늘의 최우선 후보]" in message
    assert all(token not in markdown+message for token in ("None","null","unknown"))

def test_intraday_load_does_not_recalculate_or_change_hash(tmp_path):
    generated=service(tmp_path).generate_market_close(DAY)
    loaded=service(tmp_path,[]).load_intraday(DAY)
    assert loaded["content_hash"]==generated.content_hash
    assert [(x["rank"],x["code"],x["grade"]) for x in loaded["strong"]] == [
        (x["rank"],x["code"],x["grade"]) for x in generated.report["strong"]
    ]
