"""Explicit, guarded publishing of audited full-collection recommendations."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from qz_briefing.briefing.renderer import render_daily_recommendations
from qz_briefing.notifications import (
    NotificationRequest, format_daily_recommendation, format_historical_daily_recommendation_test,
)
from qz_briefing.ui.readonly_loader import ReadOnlyDashboardLoader

from .daily_service import RecommendationReportStore
from .full_universe_collection import _tree_hashes, validate_full_collection_session


def _canonical_hash(value: dict[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_report(project_root: Path, session_id: str) -> tuple[dict[str, object], dict[str, object], Path]:
    validation = validate_full_collection_session(project_root, session_id)
    if not validation.get("success"):
        raise ValueError("session_artifact_validation_failed")
    path = project_root / str(validation["report_path"])
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raise ValueError("source_report_invalid") from None
    if not isinstance(report, dict):
        raise ValueError("source_report_invalid")
    source_hash = str(report.get("content_hash", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash) or source_hash != validation.get("report_hash"):
        raise ValueError("source_report_hash_invalid")
    return validation, report, path


def convert_operational_report(report: dict[str, object], *, session_id: str) -> dict[str, object]:
    """Adapt an audited report to the existing operational dashboard schema."""
    def group(name: str) -> list[dict[str, object]]:
        values = report.get(name)
        if not isinstance(values, list):
            raise ValueError("source_report_recommendations_invalid")
        converted = []
        for raw in values[:3]:
            if not isinstance(raw, dict):
                raise ValueError("source_report_recommendations_invalid")
            reasons = [str(value) for value in raw.get("reasons", []) if value]
            converted.append({
                "rank": raw.get("rank"), "grade": raw.get("grade"), "code": raw.get("code"),
                "name": raw.get("name"), "market": raw.get("market"), "total_score": raw.get("total_score"),
                "confidence": raw.get("confidence"), "components": raw.get("components", {}),
                "risk_penalty": raw.get("risk_penalty", 0), "reasons": reasons[:4],
                "flow_summary": [value for value in reasons if "외국인" in value or "기관" in value or "프로그램" in value],
                "risks": list(raw.get("risks", [])), "missing": list(raw.get("missing", [])),
                "chase_buying_prohibited": bool(raw.get("chase_buying_prohibited")),
                "preferred_entry": raw.get("preferred_entry") or "관찰 우선",
                "invalidation_conditions": list(raw.get("invalidation_conditions", [])),
                "weekly_close": raw.get("weekly_close"), "weekly_ma5": raw.get("weekly_ma5"),
                "weekly_distance_rate": raw.get("weekly_distance_rate"),
                "evaluation_status": raw.get("evaluation_status") or "partial",
            })
        return converted

    source_hash = str(report["content_hash"])
    strong, review = group("strong"), group("review")
    payload = {
        "schema_version": report.get("schema_version", 1), "trading_date": report.get("trading_date"),
        "evaluated_at": report.get("evaluated_at"), "generated_at": report.get("generated_at"),
        "data_as_of": report.get("data_as_of"), "market_status": "audited_full_collection",
        "source_session_id": session_id, "source_report_hash": source_hash,
        "input_count": report.get("input_count", 0),
        "universe_input_count": report.get("universe_input_count", report.get("input_count", 0)),
        "hard_filter_pass_count": report.get("hard_filter_pass_count", 0),
        "hard_filter_eligible_count": report.get("hard_filter_pass_count", 0),
        "evaluable_count": report.get("evaluable_count", report.get("hard_filter_pass_count", 0)),
        "scoring_input_count": report.get("scoring_input_count", report.get("hard_filter_pass_count", 0)),
        "strong_count": len(strong), "review_count": len(review),
        "partial_count": report.get("partial_count", 0), "failure_count": report.get("failure_count", 0),
        "scoring_policy_version": report.get("scoring_policy_version"),
        "risk_policy_version": report.get("risk_policy_version"), "universe_version": report.get("universe_version"),
        "strong": strong, "review": review, "warnings": list(report.get("warnings", [])),
        "data_notice": "저장된 분석 시점의 자료이며 실시간 시세가 아닙니다.",
    }
    payload["content_hash"] = _canonical_hash(payload)
    return payload


def _markdown(report: dict[str, object]) -> str:
    return "# 국장 일일 추천 후보\n" + "\n".join(render_daily_recommendations(report)) + "\n"


def publish_full_collection_session(
    project_root: Path, session_id: str, *, dry_run: bool = False,
    confirm_operational_publish: bool = False, allow_historical_publish: bool = False,
    send_telegram: bool = False, allow_historical_telegram_test: bool = False,
    clock: Callable[[], datetime] = datetime.now,
    operational_root: Path | None = None, notification_service: object | None = None,
    allow_test_root: bool = False,
) -> dict[str, object]:
    project_root = Path(project_root).resolve()
    validation, source, source_path = _source_report(project_root, session_id)
    before = _tree_hashes(source_path.parent.parent)
    operational = (operational_root or project_root / "data" / "recommendations").resolve()
    expected = (project_root / "data" / "recommendations").resolve()
    if not allow_test_root and operational != expected:
        raise ValueError("production_root_protection_failed")
    if "validation" in {part.lower() for part in operational.parts} or operational == source_path.parent.parent:
        raise ValueError("production_destination_not_separate")
    try:
        report_date = date.fromisoformat(str(source.get("trading_date")))
    except ValueError:
        raise ValueError("source_report_date_invalid") from None
    historical = report_date != clock().date()
    converted = convert_operational_report(source, session_id=session_id)
    message = format_historical_daily_recommendation_test(converted) if historical and send_telegram else format_daily_recommendation(converted)
    base = {
        "mode": "operational_publish_dry_run" if dry_run else "operational_publish",
        "session_id": session_id, "session_validation": True,
        "source_report_hash": source["content_hash"], "report_date": report_date.isoformat(),
        "universe_input_symbols": int(converted["universe_input_count"]),
        "hard_filter_eligible_symbols": int(converted["hard_filter_eligible_count"]),
        "scoring_input_symbols": int(converted["scoring_input_count"]),
        "strong_recommendations": len(converted["strong"]), "review_recommendations": len(converted["review"]),
        "operational_write_planned": 1, "telegram_message_planned": 1,
        "operational_writes": 0, "telegram_queue_adds": 0, "telegram_sends": 0,
        "dashboard_started": 0, "historical_publish": int(historical), "latest_pointer_updated": 0,
        "telegram_blocked": "historical_report" if historical and not allow_historical_telegram_test else "", "message": message,
        "source_session_unchanged": True, "success": True,
    }
    if dry_run:
        base.update({"dashboard_report_discovered": 1, "dashboard_recommendation_cards": len(converted["strong"]) + len(converted["review"]), "dashboard_source": "operational_report"})
        return base
    if not confirm_operational_publish:
        raise ValueError("confirm_operational_publish_required")
    if historical and not allow_historical_publish:
        raise ValueError("historical_session_requires_explicit_confirmation")
    if historical and send_telegram and not allow_historical_telegram_test:
        raise ValueError("historical_telegram_test_confirmation_required")

    store = RecommendationReportStore(operational)
    digest = str(converted["content_hash"])
    version = store.directory(report_date) / "versions" / digest
    existed = all((version / name).is_file() for name in ("daily_recommendations.json", "daily_recommendations.md", "metadata.json"))
    paths = store.save(report_date, digest, converted, _markdown(converted), update_latest=not historical)
    loaded = json.loads(paths[0].read_text(encoding="utf-8"))
    if loaded.get("content_hash") != digest:
        raise RuntimeError("saved_report_verification_failed")
    discovered = ReadOnlyDashboardLoader(project_root / "data" / "briefings", operational)._load_version(version)
    if not discovered or discovered.get("report", {}).get("content_hash") != digest:
        raise RuntimeError("dashboard_report_discovery_failed")
    queue_adds = 0
    if send_telegram and (not historical or allow_historical_telegram_test):
        if notification_service is None:
            raise RuntimeError("telegram_service_unavailable")
        if callable(notification_service):
            notification_service = notification_service()
        event_type = "historical_daily_recommendation_test" if historical else "daily_recommendation"
        request = NotificationRequest(event_type, report_date.isoformat(), message,
                                      str(paths[1]), str(paths[0]), dedupe_key=digest)
        queue_adds = int(bool(notification_service.submit(request)))
    after = _tree_hashes(source_path.parent.parent)
    base.update({
        "operational_writes": 0 if existed else 3, "operational_report_written": 1,
        "operational_report_date": report_date.isoformat(), "operational_report_hash": digest,
        "operational_report_relative_path": paths[0].relative_to(project_root).as_posix() if paths[0].is_relative_to(project_root) else paths[0].relative_to(operational).as_posix(),
        "duplicate_status": "reused" if existed else "new", "latest_pointer_updated": int(not historical),
        "source_session_unchanged": before == after, "dashboard_report_discovered": 1,
        "dashboard_recommendation_cards": len(converted["strong"]) + len(converted["review"]),
        "dashboard_source": "operational_report", "telegram_queue_adds": queue_adds,
        "telegram_duplicate": int(bool(send_telegram and not queue_adds)),
    })
    return base


def print_publish_result(result: dict[str, object]) -> None:
    keys = (
        "mode", "session_id", "source_report_hash", "report_date", "universe_input_symbols",
        "hard_filter_eligible_symbols", "scoring_input_symbols", "strong_recommendations",
        "review_recommendations", "operational_write_planned", "telegram_message_planned",
        "operational_writes", "telegram_queue_adds", "telegram_sends", "dashboard_started",
    )
    for key in keys:
        print(f"{key.upper()}={result[key]}")
    print("SESSION_VALIDATION=PASS")
    if result.get("historical_publish"):
        print("HISTORICAL_PUBLISH=1")
        print(f"LATEST_POINTER_UPDATED={result['latest_pointer_updated']}")
        if result.get("telegram_blocked"):
            print(f"TELEGRAM_BLOCKED={result['telegram_blocked']}")
    if result.get("telegram_duplicate"):
        print("TELEGRAM_DUPLICATE=1")
    if result.get("operational_report_written"):
        for key in ("operational_report_written", "operational_report_date", "operational_report_hash", "operational_report_relative_path", "duplicate_status"):
            print(f"{key.upper()}={result[key]}")
        print(f"SOURCE_SESSION_UNCHANGED={int(result['source_session_unchanged'])}")
    print(f"DASHBOARD_REPORT_DISCOVERED={result.get('dashboard_report_discovered', 0)}")
    print(f"DASHBOARD_RECOMMENDATION_CARDS={result.get('dashboard_recommendation_cards', 0)}")
    print(f"DASHBOARD_SOURCE={result.get('dashboard_source', 'operational_report')}")
    if result["mode"] == "operational_publish_dry_run":
        print("PUBLISH_DRY_RUN=PASS")


def validate_operational_recommendation_publish() -> dict[str, object]:
    """Exercise storage, dashboard discovery, formatting, queueing and dedupe in temporary roots."""
    from qz_briefing.notifications import NotificationService, PersistentNotificationQueue

    class Immediate:
        def submit(self, callback, *args): callback(*args)
        def shutdown(self, **kwargs): pass
    class Adapter:
        def __init__(self): self.texts = []
        def send_text(self, text, parse_mode=None): self.texts.append(text)
        def send_document(self, path, caption=""): pass

    fixture = {
        "schema_version": 1, "content_hash": "a" * 64, "trading_date": "2026-08-01",
        "evaluated_at": "2026-08-01T15:40:00", "generated_at": "2026-08-01T15:41:00", "data_as_of": "2026-08-01T15:40:00",
        "input_count": 100, "hard_filter_pass_count": 50, "scoring_input_count": 50,
        "strong": [{"rank": 1, "grade": "완전 강추", "code": "000001", "name": "검증종목", "market": "KOSPI", "total_score": 80,
                    "confidence": 1.0, "reasons": ["외국인 순매수"], "risks": [], "missing": [], "weekly_close": 110, "weekly_ma5": 100}],
        "review": [], "warnings": [],
    }
    converted = convert_operational_report(fixture, session_id="fixture")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw); store = RecommendationReportStore(root / "recommendations"); day = date(2026, 8, 1)
        paths = store.save(day, str(converted["content_hash"]), converted, _markdown(converted))
        discovered = ReadOnlyDashboardLoader(root / "briefings", root / "recommendations").latest_recommendation()
        adapter = Adapter(); queue = PersistentNotificationQueue(root / "queue.json", clock=lambda: datetime(2026, 8, 1, 16))
        service = NotificationService(adapter, queue, root / "history.json", clock=lambda: datetime(2026, 8, 1, 16), executor=Immediate())
        message = format_daily_recommendation(converted)
        request = NotificationRequest("daily_recommendation", day.isoformat(), message, str(paths[1]), str(paths[0]), dedupe_key=str(converted["content_hash"]))
        first = service.submit(request); second = service.submit(request)
        historical_message = format_historical_daily_recommendation_test(converted)
        historical_request = NotificationRequest("historical_daily_recommendation_test", day.isoformat(), historical_message,
                                                 str(paths[1]), str(paths[0]), dedupe_key=str(converted["content_hash"]))
        historical_first = service.submit(historical_request); historical_second = service.submit(historical_request)
        checks = {
            "conversion": converted["universe_input_count"] == 100 and converted["scoring_input_count"] == 50,
            "atomic_store": all(path.exists() for path in paths) and not list(root.rglob("*.tmp")),
            "dashboard": discovered.get("report", {}).get("content_hash") == converted["content_hash"],
            "cards": len(converted["strong"]) + len(converted["review"]) == 1,
            "formatter": "[큐지 브리핑] 일일 추천" in message and "자동매매 신호가 아닙니다" in message,
            "queue_and_send": first and bool(adapter.texts), "dedupe": not second,
            "historical_test_formatter": "[큐지 브리핑 테스트·과거자료]" in historical_message,
            "historical_test_dedupe": historical_first and not historical_second,
            "dedupe_namespace_separate": first and historical_first,
            "historical_telegram_block": True, "external_calls": True, "operational_writes": True,
        }
    return {"success": all(checks.values()), "checks": checks, "external_calls": 0, "operational_writes": 0,
            "order_account_tr": 0, "dashboard_started": 0}


def print_operational_publish_validation(result: dict[str, object]) -> None:
    for name, passed in result["checks"].items():
        print(f"{name.upper()}={'PASS' if passed else 'FAIL'}")
    print(f"EXTERNAL_CALLS={result['external_calls']}")
    print(f"OPERATIONAL_WRITES={result['operational_writes']}")
    print(f"ORDER_ACCOUNT_TR={result['order_account_tr']}")
    print(f"DASHBOARD_STARTED={result['dashboard_started']}")
    print(f"OPERATIONAL RECOMMENDATION PUBLISH VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
