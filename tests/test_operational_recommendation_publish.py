import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from qz_briefing.__main__ import parse_cli_arguments
from qz_briefing.notifications import NotificationRequest, NotificationService, PersistentNotificationQueue
from qz_briefing.notifications.formatter import (
    format_daily_recommendation, format_historical_daily_recommendation_test,
    format_saved_time, format_won,
)
from qz_briefing.recommendations.daily_service import RecommendationReportStore
from qz_briefing.recommendations.operational_publish import (
    convert_operational_report, publish_full_collection_session, validate_operational_recommendation_publish,
)
from qz_briefing.ui.readonly_loader import ReadOnlyDashboardLoader
from qz_briefing.ui.main_window import DashboardMainWindow
from PyQt5.QtWidgets import QApplication, QLabel


def source_report(day="2026-08-01", digest="a" * 64):
    row = {
        "rank": 1, "grade": "강추·추가 검토", "code": "000080", "name": "하이트진로", "market": "KOSPI",
        "total_score": 70.8, "confidence": 1.0, "components": {}, "risk_penalty": 0,
        "reasons": ["마지막 완성 주봉 종가가 5주선 위", "외국인 최근 5일 순매수 3,071"],
        "risks": [], "missing": ["검증된 재료·실적 자료 부족"], "chase_buying_prohibited": False,
        "preferred_entry": "관찰 우선", "invalidation_conditions": [], "weekly_close": 15250,
        "weekly_ma5": 14902, "weekly_distance_rate": 2.33, "evaluation_status": "partial",
    }
    return {
        "schema_version": 1, "content_hash": digest, "trading_date": day,
        "evaluated_at": f"{day}T15:40:00", "generated_at": f"{day}T15:41:00", "data_as_of": f"{day}T15:40:00",
        "input_count": 100, "hard_filter_pass_count": 50, "scoring_input_count": 50,
        "strong": [], "review": [row], "partial_count": 1, "failure_count": 0,
        "scoring_policy_version": "integrated-v1", "risk_policy_version": "risk-v1",
        "universe_version": "common-stock-v1", "warnings": [],
    }


def install_source(monkeypatch, root: Path, report: dict):
    path = root / "data" / "validation" / "recommendations" / "full_collection" / "fixture" / "reports" / "recommendations.json"
    path.parent.mkdir(parents=True); path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    validation = {"success": True, "report_path": path.relative_to(root).as_posix(), "report_hash": report["content_hash"]}
    monkeypatch.setattr("qz_briefing.recommendations.operational_publish.validate_full_collection_session", lambda *_: validation)
    return path


def publish(monkeypatch, tmp_path, report=None, **kwargs):
    install_source(monkeypatch, tmp_path, report or source_report())
    return publish_full_collection_session(
        tmp_path, "fixture", operational_root=tmp_path / "operational", allow_test_root=True,
        clock=lambda: datetime(2026, 8, 1, 16), **kwargs,
    )


def test_cli_requires_session_id_and_accepts_explicit_publish_options():
    parsed = parse_cli_arguments(["--publish-full-collection-session", "--session-id", "fixture", "--dry-run"])
    assert parsed.publish_full_collection_session and parsed.session_id == "fixture" and parsed.resume is None
    assert parse_cli_arguments(["--validate-operational-recommendation-publish"]).validate_operational_recommendation_publish


def test_dry_run_plans_report_dashboard_and_telegram_without_writes(monkeypatch, tmp_path):
    result = publish(monkeypatch, tmp_path, dry_run=True)
    assert result["success"] and result["operational_writes"] == result["telegram_queue_adds"] == result["telegram_sends"] == 0
    assert result["dashboard_report_discovered"] == 1 and result["dashboard_started"] == 0
    assert not (tmp_path / "operational").exists()


def test_confirmation_is_required_before_operational_write(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="confirm_operational_publish_required"):
        publish(monkeypatch, tmp_path)
    assert not (tmp_path / "operational").exists()


def test_historical_report_requires_separate_confirmation(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="historical_session_requires_explicit_confirmation"):
        publish(monkeypatch, tmp_path, report=source_report("2026-07-30"), confirm_operational_publish=True)


def test_historical_publish_preserves_original_date_and_does_not_update_latest(monkeypatch, tmp_path):
    result = publish(monkeypatch, tmp_path, report=source_report("2026-07-30"), confirm_operational_publish=True, allow_historical_publish=True)
    assert result["historical_publish"] == 1 and result["latest_pointer_updated"] == 0
    assert result["telegram_blocked"] == "historical_report" and result["telegram_queue_adds"] == 0
    assert not (tmp_path / "operational" / "reports" / "2026-07-30" / "latest.json").exists()


def test_explicit_report_date_discovers_history_without_latest(monkeypatch, tmp_path):
    publish(monkeypatch, tmp_path, report=source_report("2026-07-30"), confirm_operational_publish=True, allow_historical_publish=True)
    loader = ReadOnlyDashboardLoader(tmp_path / "briefings", tmp_path / "operational")
    loaded = loader.recommendation_for_date(date(2026, 7, 30))
    assert loaded["historical"] and loaded["selected_report_date"] == "2026-07-30"
    assert loaded["report"]["review"][0]["code"] == "000080"


def test_conversion_separates_universe_and_scoring_and_caps_groups():
    report = source_report(); report["strong"] = report["review"] * 5; report["review"] = report["review"] * 5
    converted = convert_operational_report(report, session_id="fixture")
    assert converted["universe_input_count"] == 100 and converted["scoring_input_count"] == 50
    assert len(converted["strong"]) == len(converted["review"]) == 3
    assert converted["source_session_id"] == "fixture" and converted["source_report_hash"] == "a" * 64


def test_conversion_retains_dashboard_fields_flow_missing_and_data_notice():
    row = convert_operational_report(source_report(), session_id="fixture")["review"][0]
    assert row["code"] == "000080" and row["weekly_close"] == 15250 and row["weekly_ma5"] == 14902
    assert row["flow_summary"] == ["외국인 최근 5일 순매수 3,071"]
    assert "검증된 재료·실적 자료 부족" in row["missing"]


def test_atomic_store_reuses_same_hash_and_preserves_different_versions(tmp_path):
    store = RecommendationReportStore(tmp_path); day = datetime(2026, 8, 1).date()
    one = convert_operational_report(source_report(), session_id="one")
    paths = store.save(day, one["content_hash"], one, "one")
    mtimes = [path.stat().st_mtime_ns for path in paths]
    store.save(day, one["content_hash"], one, "one")
    assert [path.stat().st_mtime_ns for path in paths] == mtimes
    two = convert_operational_report(source_report("2026-08-01", "b" * 64), session_id="two")
    store.save(day, two["content_hash"], two, "two")
    assert len(list((tmp_path / "reports" / day.isoformat() / "versions").iterdir())) == 2
    assert not list(tmp_path.rglob("*.tmp"))


def test_publish_is_discovered_by_existing_readonly_loader(monkeypatch, tmp_path):
    result = publish(monkeypatch, tmp_path, confirm_operational_publish=True)
    loaded = ReadOnlyDashboardLoader(tmp_path / "briefings", tmp_path / "operational").latest_recommendation()
    assert result["dashboard_recommendation_cards"] == 1
    assert loaded["report"]["content_hash"] == result["operational_report_hash"]


def test_source_validation_tree_is_unchanged(monkeypatch, tmp_path):
    source = source_report(); path = install_source(monkeypatch, tmp_path, source); before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = publish_full_collection_session(tmp_path, "fixture", confirm_operational_publish=True,
        operational_root=tmp_path / "operational", allow_test_root=True, clock=lambda: datetime(2026, 8, 1, 16))
    assert result["source_session_unchanged"] and hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_formatter_contains_required_sections_warning_and_no_internal_path():
    report = convert_operational_report(source_report(), session_id="fixture")
    text = format_daily_recommendation(report)
    for token in ("[큐지 브리핑] 일일 추천", "전체 검토 종목", "주봉 MA5 통과 종목", "추가 검토", "자동매매 신호가 아닙니다", "추격매수"):
        assert token in text
    assert "validation" not in text and "C:\\Users" not in text


def test_operational_price_and_timestamp_display_formats_are_safe_and_exact():
    assert format_won(15250.0) == "15,250원"
    assert format_won(14902.0) == "14,902원"
    assert format_won(4035.0) == "4,035원"
    assert format_won(15250.25) == "15,250.25원"
    assert format_won(None) == format_won("broken") == "자료 없음"
    assert format_saved_time("2026-07-30T10:16:37.457636") == "2026-07-30 10:16:37"
    assert format_saved_time(None) == "자료 없음"


def test_telegram_uses_price_and_second_timestamp_format_without_changing_report_hash():
    report = convert_operational_report(source_report("2026-07-30"), session_id="fixture")
    report["data_as_of"] = "2026-07-30T10:16:37.457636"
    digest = report["content_hash"]
    text = format_daily_recommendation(report)
    assert "주봉 종가 / MA5: 15,250원 / 14,902원" in text
    assert text.count("2026-07-30 10:16:37") == 2
    assert ".0 /" not in text and ".457636" not in text and "T10:16" not in text
    assert report["content_hash"] == digest
    request = NotificationRequest("historical_daily_recommendation_test", "2026-07-30", text, dedupe_key=digest)
    assert request.dedupe_key == digest


class Immediate:
    def submit(self, callback, *args): callback(*args)
    def shutdown(self, **kwargs): pass


class Adapter:
    def __init__(self, fail=False): self.texts = []; self.fail = fail
    def send_text(self, text, parse_mode=None):
        if self.fail: raise TimeoutError("fixture")
        self.texts.append(text)
    def send_document(self, path, caption=""): pass


def test_existing_notification_service_uses_report_hash_dedupe(tmp_path):
    adapter = Adapter(); queue = PersistentNotificationQueue(tmp_path / "queue.json", clock=lambda: datetime(2026, 8, 1, 16))
    service = NotificationService(adapter, queue, tmp_path / "history.json", clock=lambda: datetime(2026, 8, 1, 16), executor=Immediate())
    request = NotificationRequest("daily_recommendation", "2026-08-01", "message", dedupe_key="report-hash")
    assert service.submit(request) and not service.submit(request) and len(adapter.texts) == 1


def test_send_flag_controls_mock_queue_and_historical_never_queues(monkeypatch, tmp_path):
    class Service:
        def __init__(self): self.requests = []
        def submit(self, request): self.requests.append(request); return True
    service = Service()
    result = publish(monkeypatch, tmp_path, confirm_operational_publish=True, send_telegram=True, notification_service=service)
    assert result["telegram_queue_adds"] == 1 and service.requests[0].dedupe_key == result["operational_report_hash"]
    service2 = Service()
    with pytest.raises(ValueError, match="historical_telegram_test_confirmation_required"):
        publish(monkeypatch, tmp_path / "old", report=source_report("2026-07-30"), confirm_operational_publish=True,
                allow_historical_publish=True, send_telegram=True, notification_service=service2)
    assert not service2.requests


def test_historical_telegram_test_requires_both_flags_and_uses_separate_event(monkeypatch, tmp_path):
    class Service:
        def __init__(self): self.requests = []
        def submit(self, request): self.requests.append(request); return True
    service = Service()
    result = publish(monkeypatch, tmp_path, report=source_report("2026-07-30"), confirm_operational_publish=True,
                     allow_historical_publish=True, send_telegram=True, allow_historical_telegram_test=True,
                     notification_service=service)
    assert result["telegram_queue_adds"] == 1 and result["dashboard_started"] == 0
    assert service.requests[0].event_type == "historical_daily_recommendation_test"
    assert service.requests[0].dedupe_key == result["operational_report_hash"]
    assert "[큐지 브리핑 테스트·과거자료] 일일 추천 2026-07-30" in service.requests[0].text
    assert all(token in service.requests[0].text for token in ("테스트 전송", "과거 자료", "실시간 추천 아님"))


def test_historical_and_normal_telegram_dedupe_namespaces_are_distinct(tmp_path):
    adapter = Adapter(); queue = PersistentNotificationQueue(tmp_path / "queue.json", clock=lambda: datetime(2026, 8, 1, 16))
    service = NotificationService(adapter, queue, tmp_path / "history.json", clock=lambda: datetime(2026, 8, 1, 16), executor=Immediate())
    normal = NotificationRequest("daily_recommendation", "2026-07-30", "normal", dedupe_key="same-hash")
    historical = NotificationRequest("historical_daily_recommendation_test", "2026-07-30", "historical", dedupe_key="same-hash")
    assert service.submit(normal) and service.submit(historical)
    assert not service.submit(historical) and len(adapter.texts) == 2


def test_historical_formatter_has_unmistakable_test_header():
    report = convert_operational_report(source_report("2026-07-30"), session_id="fixture")
    text = format_historical_daily_recommendation_test(report)
    assert text.startswith("[큐지 브리핑 테스트·과거자료] 일일 추천 2026-07-30")
    assert "하이트진로(000080)" in text and "검증된 재료·실적 자료 부족" in text


def test_without_send_flag_no_notification_service_is_constructed(monkeypatch, tmp_path):
    result = publish(monkeypatch, tmp_path, confirm_operational_publish=True,
                     notification_service=lambda: pytest.fail("must not construct Telegram service"))
    assert result["telegram_queue_adds"] == result["telegram_sends"] == 0


def test_corrupt_report_and_bad_hash_are_blocked(monkeypatch, tmp_path):
    path = install_source(monkeypatch, tmp_path, source_report()); path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="source_report_invalid"):
        publish_full_collection_session(tmp_path, "fixture", dry_run=True)
    report = source_report(digest="bad"); install_source(monkeypatch, tmp_path / "bad", report)
    with pytest.raises(ValueError, match="source_report_hash_invalid"):
        publish_full_collection_session(tmp_path / "bad", "fixture", dry_run=True)


def test_validation_cli_exercises_mock_storage_dashboard_telegram_and_dedupe():
    result = validate_operational_recommendation_publish()
    assert result["success"] and result["external_calls"] == result["operational_writes"] == result["order_account_tr"] == 0
    assert result["checks"]["dashboard"] and result["checks"]["dedupe"]


def test_dashboard_report_date_renders_badge_date_and_three_cards(tmp_path):
    report = source_report("2026-07-30")
    names = (("000080", "하이트진로", 70.8), ("005710", "대원산업", 65.0), ("004780", "대륙제관", 61.6))
    report["review"] = [{**report["review"][0], "code": code, "name": name, "total_score": score, "rank": index}
                        for index, (code, name, score) in enumerate(names, 1)]
    converted = convert_operational_report(report, session_id="fixture")
    store = RecommendationReportStore(tmp_path / "recommendations")
    store.save(date(2026, 7, 30), converted["content_hash"], converted, "history", update_latest=False)
    app = QApplication.instance() or QApplication([])
    window = DashboardMainWindow(tmp_path / "briefings", recommendation_root=tmp_path / "recommendations",
        report_date=date(2026, 7, 30), connection_state=lambda: "DISCONNECTED", trading_day_status="장 종료",
        shutdown=lambda: None, open_folder=lambda: None, read_only=True, standalone=True,
        clock=lambda: datetime(2026, 8, 1, 12))
    texts = "\n".join(label.text() for label in window.findChildren(QLabel))
    assert len(window._recommendation_cards) == 3
    assert all(token in texts for token in ("과거 보고서", "기준일: 2026-07-30", "실시간 자료 아님", "하이트진로", "대원산업", "대륙제관"))
    assert "수급 요약" in texts and "저장 시각" in texts
    assert "15,250원" in texts and "14,902원" in texts and "2.3%" in texts
    assert "2026-07-30 15:40:00" in texts and "T15:40" not in texts
    window.stop(); window.close(); window._test_app = app
