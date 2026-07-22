"""Fault-isolated daily briefing execution pipeline."""

from __future__ import annotations

import logging
import copy
from collections.abc import Callable, Sequence
from datetime import date, datetime

from .collectors import BriefingCollector
from .analysis import analyze_briefing
from .leadership import score_leader
from .rules import index_rates
from .models import (
    SCHEMA_VERSION,
    BriefingContext,
    BriefingRunResult,
    BriefingType,
)
from .renderer import render_markdown
from .storage import BriefingStorage


FINAL_STATUSES = {"completed", "completed_with_errors"}
LOGGER = logging.getLogger(__name__)


class DailyBriefingPipeline:
    def __init__(
        self,
        storage: BriefingStorage,
        collectors: Sequence[BriefingCollector],
        *,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._storage = storage
        self._collectors = tuple(collectors)
        self._clock = clock
        self._in_progress: set[tuple[date, BriefingType]] = set()
        self._completed: set[tuple[date, BriefingType]] = set()

    def run(
        self,
        briefing_type: BriefingType,
        trading_date: date,
        *,
        market_calendar_status: str,
        market_calendar_reason: str,
        market_calendar_warning: str | None = None,
    ) -> BriefingRunResult:
        key = (trading_date, briefing_type)
        if key in self._in_progress or key in self._completed:
            print("briefing already completed", flush=True)
            return BriefingRunResult("skipped", briefing_type, trading_date)

        completed, existing_warning = self._validate_existing_result(
            trading_date, briefing_type
        )
        if completed:
            self._completed.add(key)
            print("briefing already completed", flush=True)
            return BriefingRunResult("skipped", briefing_type, trading_date)

        self._in_progress.add(key)
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

            print(f"briefing pipeline started: {briefing_type.value}", flush=True)
            prior_pre_market = self._load_pre_market(context)
            collector_results: dict[str, dict[str, object]] = {}
            for collector in self._collectors:
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
            status = "completed_with_errors" if context.errors else "completed"
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
            pre_market_source = None
            if briefing_type is BriefingType.INTRADAY_10AM:
                try:
                    pre_market_source = self._storage.load_json(
                        trading_date, BriefingType.PRE_MARKET
                    )
                except (OSError, ValueError):
                    pre_market_source = None
            result["analysis"] = analyze_briefing(result, pre_market_source)
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
                if pre_market_source and isinstance(
                    pre_market_source.get("holdings_analysis"), dict
                ):
                    result["holdings_analysis"]["comparison_with_pre_market"] = (
                        compare_holdings(
                            result["holdings_analysis"],
                            pre_market_source["holdings_analysis"],
                        )
                    )
            json_path, markdown_path = self._storage.save(
                trading_date, briefing_type, result, render_markdown(result)
            )
            self._completed.add(key)
            print(f"briefing result saved: {json_path}", flush=True)
            print(f"briefing pipeline completed: {briefing_type.value}", flush=True)
            return BriefingRunResult(
                status,
                briefing_type,
                trading_date,
                str(json_path),
                str(markdown_path),
            )
        finally:
            self._in_progress.discard(key)

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
