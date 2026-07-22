"""Fault-isolated daily briefing execution pipeline."""

from __future__ import annotations

import logging
import copy
from collections.abc import Callable, Sequence
from datetime import date, datetime, time

from .collectors import BriefingCollector
from .analysis import analyze_briefing
from .leadership import score_leader
from .market_close import (
    build_next_session_watchlist, compare_market_close, evaluate_market_close,
)
from .rules import index_rates
from .models import (
    SCHEMA_VERSION,
    BriefingContext,
    BriefingRunResult,
    BriefingType,
)
from .renderer import render_markdown
from .decision_guidance import holding_decision, market_decision, priority
from .storage import BriefingStorage


FINAL_STATUSES = {"completed", "completed_with_errors", "no_market_open"}
LOGGER = logging.getLogger(__name__)


class DailyBriefingPipeline:
    def __init__(
        self,
        storage: BriefingStorage,
        collectors: Sequence[BriefingCollector],
        *,
        clock: Callable[[], datetime] = datetime.now,
        preopen_monitoring: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._storage = storage
        self._collectors = tuple(collectors)
        self._clock = clock
        self._preopen_monitoring = preopen_monitoring
        self._in_progress: set[tuple[date, BriefingType, bool]] = set()
        self._completed: set[tuple[date, BriefingType]] = set()
        self._completion_listeners: list[Callable[[str, str], None]] = []

    @property
    def storage_root(self):
        return self._storage.root

    def add_completion_listener(self, listener: Callable[[str, str], None]) -> None:
        if listener not in self._completion_listeners:
            self._completion_listeners.append(listener)

    def set_preopen_monitoring_provider(
        self, provider: Callable[[], dict[str, object]]
    ) -> None:
        self._preopen_monitoring = provider

    def run(
        self,
        briefing_type: BriefingType,
        trading_date: date,
        *,
        market_calendar_status: str,
        market_calendar_reason: str,
        market_calendar_warning: str | None = None,
        manual_validation: bool = False,
    ) -> BriefingRunResult:
        key = (trading_date, briefing_type)
        progress_key = (trading_date, briefing_type, manual_validation)
        if progress_key in self._in_progress or (not manual_validation and key in self._completed):
            print(f"briefing already completed: {briefing_type.value}", flush=True)
            return BriefingRunResult("skipped", briefing_type, trading_date)

        completed, existing_warning = (False, None) if manual_validation else self._validate_existing_result(trading_date, briefing_type)
        if completed:
            self._completed.add(key)
            print(f"briefing already completed: {briefing_type.value}", flush=True)
            return BriefingRunResult("skipped", briefing_type, trading_date)

        self._in_progress.add(progress_key)
        try:
            requested_at = self._clock()
            context = BriefingContext(
                briefing_type=briefing_type,
                trading_date=trading_date,
                requested_at=requested_at,
                started_at=self._clock(),
                market_calendar_status=market_calendar_status,
                market_calendar_reason=market_calendar_reason,
                market_calendar_warning=market_calendar_warning,
            )
            if market_calendar_warning:
                context.warnings.append(market_calendar_warning)
            if existing_warning:
                context.warnings.append(existing_warning)
            if manual_validation and context.started_at.time() < time(15, 40):
                context.warnings.append(
                    "장 종료 전 수동 검증 결과이며 실제 장마감 데이터가 아닙니다."
                )

            monitoring = None
            if self._preopen_monitoring is not None:
                monitoring = self._preopen_monitoring()
            no_market_open = bool(
                monitoring
                and not monitoring.get("market_open_detected")
                and context.started_at.time() >= time(9, 5)
            )
            if no_market_open:
                context.warnings.append("공식 시장 개시 신호가 확인되지 않았습니다.")

            log_name = f"{briefing_type.value} validation" if manual_validation else briefing_type.value
            print(f"briefing pipeline started: {log_name}", flush=True)
            prior_pre_market = self._load_pre_market(context)
            collector_results: dict[str, dict[str, object]] = {}
            for collector in (() if no_market_open else self._collectors):
                print(f"briefing collector started: {collector.name}", flush=True)
                try:
                    data = collector.collect(context)
                    collector_errors = (
                        data.get("errors", []) if isinstance(data, dict) else []
                    )
                    if collector_errors:
                        context.errors.extend(
                            f"{collector.name}: {error}" for error in collector_errors
                        )
                    collector_results[collector.name] = {
                        "status": "error" if collector_errors else "success",
                        "data": data,
                        "error": None,
                    }
                    print(
                        f"briefing collector completed: {collector.name}", flush=True
                    )
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    context.errors.append(f"{collector.name}: {error}")
                    collector_results[collector.name] = {
                        "status": "error",
                        "data": {},
                        "error": error,
                    }
                    LOGGER.exception("briefing collector failed: %s", collector.name)
                    print(f"briefing collector failed: {collector.name}", flush=True)

            context.completed_at = self._clock()
            status = "no_market_open" if no_market_open else (
                "completed_with_errors" if context.errors else "completed"
            )
            result: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "briefing_type": briefing_type.value,
                "trading_date": trading_date.isoformat(),
                "requested_at": context.requested_at.isoformat(),
                "started_at": context.started_at.isoformat(),
                "completed_at": context.completed_at.isoformat(),
                "status": status,
                "market_calendar": {
                    "status": market_calendar_status,
                    "reason": market_calendar_reason,
                    "warning": market_calendar_warning,
                },
                "collectors": collector_results,
                "pre_market_result": prior_pre_market,
                "warnings": context.warnings,
                "errors": context.errors,
            }
            if briefing_type is BriefingType.PRE_MARKET:
                if monitoring is None:
                    monitoring = {
                        "window_start": "08:00:00", "window_end": "09:00:00",
                        "actual_start": "", "sampling_interval_seconds": 300,
                        "sample_count": 0, "coverage_status": "not_started",
                        "market_open_detected": False, "indices": {}, "large_caps": {},
                        "holdings": [], "leaders": [], "changes": {}, "signals": [],
                        "warnings": ["preopen monitoring was not started"],
                    }
                result["preopen_monitoring"] = monitoring
                context.warnings.extend(str(item) for item in monitoring.get("warnings", []) if item)
            if no_market_open:
                result["message"] = "장이 개시되지 않아 오늘 브리핑이 없습니다."
            pre_market_source = None
            intraday_source = None
            if briefing_type is BriefingType.INTRADAY_10AM:
                try:
                    pre_market_source = self._storage.load_json(
                        trading_date, BriefingType.PRE_MARKET
                    )
                except (OSError, ValueError):
                    pre_market_source = None
            elif briefing_type is BriefingType.MARKET_CLOSE:
                pre_market_source = self._load_same_day_result(
                    context, BriefingType.PRE_MARKET, "pre-market"
                )
                intraday_source = self._load_same_day_result(
                    context, BriefingType.INTRADAY_10AM, "intraday"
                )
            result["analysis"] = (
                {"market_state": "market_not_open", "summary": result["message"], "warnings": list(context.warnings), "decision": market_decision(result)}
                if no_market_open else analyze_briefing(result, intraday_source or pre_market_source)
            )
            leadership_result = collector_results.get("kiwoom_market_leadership")
            if isinstance(leadership_result, dict) and isinstance(
                leadership_result.get("data"), dict
            ):
                result["leadership"] = build_leadership_output(
                    leadership_result["data"], index_rates(result)
                )
                if briefing_type is BriefingType.PRE_MARKET:
                    previous = self._storage.load_latest_before(
                        trading_date, BriefingType.INTRADAY_10AM
                    )
                    if previous and isinstance(previous.get("leadership"), dict):
                        result["leadership"] = copy.deepcopy(previous["leadership"])
                        result["leadership"]["basis"] = "previous_saved_result"
                        result["leadership"]["source_trading_date"] = previous.get(
                            "trading_date"
                        )
                    else:
                        result["leadership"]["basis"] = "current_available_data"
                        result["leadership"].setdefault("warnings", []).append(
                            "이전 저장 결과가 없어 현재 확인 가능한 데이터 기준입니다."
                        )
                if pre_market_source and isinstance(
                    pre_market_source.get("leadership"), dict
                ):
                    result["leadership"]["comparison_with_pre_market"] = (
                        compare_leadership(
                            result["leadership"], pre_market_source["leadership"]
                        )
                    )
            holdings_result = collector_results.get("holdings_analysis")
            if isinstance(holdings_result, dict) and isinstance(
                holdings_result.get("data"), dict
            ):
                result["holdings_analysis"] = holdings_result["data"]
                market_decision = result["analysis"].get("decision", {})
                for item in result["holdings_analysis"].get("holdings", []):
                    if isinstance(item, dict):
                        item["decision"] = holding_decision(item, market_decision)
                        item["priority"] = priority(item["decision"])
                result["holdings_analysis"]["holdings"].sort(
                    key=lambda item: (item.get("priority", 8), str(item.get("code", "")))
                )
                if pre_market_source and isinstance(
                    pre_market_source.get("holdings_analysis"), dict
                ):
                    result["holdings_analysis"]["comparison_with_pre_market"] = (
                        compare_holdings(
                            result["holdings_analysis"],
                            pre_market_source["holdings_analysis"],
                        )
                    )
            if briefing_type is BriefingType.MARKET_CLOSE:
                comparison = compare_market_close(
                    result, pre_market_source, intraday_source
                )
                result["metadata"] = {
                    "briefing_type": briefing_type.value,
                    "trading_date": trading_date.isoformat(),
                    "generated_at": context.completed_at.isoformat(),
                    "basis": "market_close",
                }
                if manual_validation:
                    result["metadata"].update({
                        "execution_mode": "manual_validation",
                        "basis": "current_market_snapshot",
                    })
                result["previous_results"] = {
                    "pre_market_loaded": pre_market_source is not None,
                    "intraday_10am_loaded": intraday_source is not None,
                    "warnings": [warning for warning in context.warnings if "result" in warning.lower()],
                }
                result["session_comparison"] = comparison
                result["market_close_analysis"] = evaluate_market_close(
                    result, comparison
                )
                result["next_session_watchlist"] = build_next_session_watchlist(result)
            elif briefing_type is BriefingType.PRE_MARKET:
                previous_close, warning = self._storage.load_recent_market_close(
                    trading_date
                )
                result["previous_market_close"] = previous_close
                if warning:
                    context.warnings.append(warning)
            save = self._storage.save_validation if manual_validation else self._storage.save
            json_path, markdown_path = save(
                trading_date, briefing_type, result, render_markdown(result)
            )
            if not manual_validation:
                self._completed.add(key)
            print(f"briefing result saved: {json_path}", flush=True)
            print(f"briefing pipeline completed: {log_name}", flush=True)
            for listener in tuple(self._completion_listeners):
                try:
                    listener(log_name, str(json_path))
                except Exception:
                    LOGGER.exception("briefing dashboard refresh notification failed")
            return BriefingRunResult(
                status,
                briefing_type,
                trading_date,
                str(json_path),
                str(markdown_path),
            )
        finally:
            self._in_progress.discard(progress_key)

    def _load_same_day_result(
        self, context: BriefingContext, briefing_type: BriefingType, label: str
    ) -> dict[str, object] | None:
        try:
            result = self._storage.load_json(context.trading_date, briefing_type)
            if result is None:
                context.warnings.append(f"{label} result not found")
                return None
            print(f"{label} result loaded", flush=True)
            return result
        except (OSError, ValueError) as exc:
            context.warnings.append(
                f"{label} result unavailable: {type(exc).__name__}: {exc}"
            )
            return None

    def _validate_existing_result(
        self, trading_date: date, briefing_type: BriefingType
    ) -> tuple[bool, str | None]:
        json_exists, markdown_exists = self._storage.result_files_exist(
            trading_date, briefing_type
        )
        if not json_exists and not markdown_exists:
            return False, None
        if not json_exists:
            return False, (
                "Existing briefing result is incomplete and will be replaced: "
                f"json_exists={json_exists}, markdown_exists={markdown_exists}"
            )
        try:
            existing = self._storage.load_json(trading_date, briefing_type)
        except (OSError, ValueError) as exc:
            return False, (
                "Existing briefing result could not be read and will be replaced: "
                f"{type(exc).__name__}: {exc}"
            )
        if not markdown_exists:
            return False, (
                "Existing briefing result is incomplete and will be replaced: "
                f"json_exists={json_exists}, markdown_exists={markdown_exists}"
            )
        expected = {
            "schema_version": SCHEMA_VERSION,
            "briefing_type": briefing_type.value,
            "trading_date": trading_date.isoformat(),
        }
        for field, expected_value in expected.items():
            if existing.get(field) != expected_value:
                return False, (
                    "Existing briefing result is invalid and will be replaced: "
                    f"{field}={existing.get(field)!r}, expected={expected_value!r}"
                )
        if existing.get("status") not in FINAL_STATUSES:
            return False, (
                "Existing briefing result is not complete and will be replaced: "
                f"status={existing.get('status')!r}"
            )
        return True, None

    def _load_pre_market(self, context: BriefingContext) -> dict[str, object] | None:
        if context.briefing_type is not BriefingType.INTRADAY_10AM:
            return None
        try:
            result = self._storage.load_json(
                context.trading_date, BriefingType.PRE_MARKET
            )
            if result is None:
                raise FileNotFoundError("pre_market.json does not exist")
            print("pre-market result loaded", flush=True)
            return {
                "exists": True,
                "status": result.get("status"),
                "completed_at": result.get("completed_at"),
            }
        except (OSError, ValueError) as exc:
            warning = f"Pre-market result unavailable: {type(exc).__name__}: {exc}"
            context.warnings.append(warning)
            print("pre-market result unavailable", flush=True)
            return {"exists": False, "status": None, "completed_at": None}


def compare_leadership(
    current: dict[str, object], previous: dict[str, object]
) -> dict[str, list[str]]:
    current_codes = {
        str(row.get("code"))
        for market in ("kospi", "kosdaq")
        for row in current.get(market, [])
        if isinstance(row, dict)
    }
    previous_codes = {
        str(row.get("code"))
        for market in ("kospi", "kosdaq")
        for row in previous.get(market, [])
        if isinstance(row, dict)
    }
    return {
        "new": sorted(current_codes - previous_codes),
        "maintained": sorted(current_codes & previous_codes),
        "dropped": sorted(previous_codes - current_codes),
    }


def build_leadership_output(
    source: dict[str, object], market_rates: dict[str, float | None]
) -> dict[str, object]:
    output = copy.deepcopy(source)
    for key, market in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        rows = output.get(key, [])
        if not isinstance(rows, list):
            continue
        rescored = [score_leader(row, market_rates.get(market)) for row in rows]
        rescored.sort(
            key=lambda row: (
                -float(row["score"]),
                int(row.get("trading_value_rank") or 9999),
                str(row["code"]),
            )
        )
        for rank, row in enumerate(rescored[:10], 1):
            row["rank"] = rank
        output[key] = rescored[:10]
    return output


def compare_holdings(
    current: dict[str, object], previous: dict[str, object]
) -> list[dict[str, object]]:
    old = {
        str(item.get("code")): item
        for item in previous.get("holdings", [])
        if isinstance(item, dict)
    }
    changes = []
    for item in current.get("holdings", []):
        if not isinstance(item, dict) or str(item.get("code")) not in old:
            continue
        prior = old[str(item.get("code"))]
        changes.append({
            "code": item.get("code"),
            "trend": {"pre_market": prior.get("trend"), "current": item.get("trend")},
            "bottom_confirmation": {"pre_market": prior.get("bottom_confirmation"), "current": item.get("bottom_confirmation")},
            "review_status": {"pre_market": prior.get("review_status"), "current": item.get("review_status")},
        })
    return changes
