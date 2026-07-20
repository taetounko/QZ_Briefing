"""Daily briefing pipeline and atomic storage tests."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from qz_briefing.briefing import (
    BriefingStorage,
    BriefingType,
    DailyBriefingPipeline,
    PlaceholderCollector,
)
from qz_briefing.briefing.models import BriefingContext


NOW = datetime(2026, 7, 21, 8, 0)
TRADING_DATE = date(2026, 7, 21)


class RecordingCollector:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.failure = failure

    def collect(self, context: BriefingContext) -> dict[str, str]:
        self.calls.append(self.name)
        if self.failure is not None:
            raise self.failure
        return {"collector": self.name, "trading_date": context.trading_date.isoformat()}


def run_pipeline(
    storage: BriefingStorage,
    collectors: list[RecordingCollector] | None = None,
    briefing_type: BriefingType = BriefingType.PRE_MARKET,
):
    pipeline = DailyBriefingPipeline(
        storage,
        collectors or [RecordingCollector("first", [])],
        clock=lambda: NOW,
    )
    result = pipeline.run(
        briefing_type,
        TRADING_DATE,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    return pipeline, result


def load_result(storage: BriefingStorage, briefing_type: BriefingType) -> dict:
    json_path, _ = storage.result_paths(TRADING_DATE, briefing_type)
    return json.loads(json_path.read_text(encoding="utf-8"))


def save_existing_result(
    storage: BriefingStorage,
    *,
    schema_version: int = 1,
    briefing_type: str = "pre_market",
    trading_date: str = "2026-07-21",
    status: str = "completed",
) -> tuple[Path, Path]:
    return storage.save(
        TRADING_DATE,
        BriefingType.PRE_MARKET,
        {
            "schema_version": schema_version,
            "briefing_type": briefing_type,
            "trading_date": trading_date,
            "status": status,
        },
        "original markdown",
    )


def test_briefing_type_values_are_centralized() -> None:
    assert BriefingType.PRE_MARKET.value == "pre_market"
    assert BriefingType.INTRADAY_10AM.value == "intraday_10am"


def test_storage_builds_date_partitioned_paths(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    json_path, markdown_path = storage.result_paths(
        TRADING_DATE, BriefingType.PRE_MARKET
    )
    expected = tmp_path / "2026" / "07" / "21"
    assert json_path == expected / "pre_market.json"
    assert markdown_path == expected / "pre_market.md"


def test_pre_market_saves_json_and_markdown(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    _, result = run_pipeline(storage)
    json_path = Path(result.json_path or "")
    markdown_path = Path(result.markdown_path or "")
    assert json_path.is_file()
    assert markdown_path.is_file()
    assert load_result(storage, BriefingType.PRE_MARKET)["status"] == "completed"
    assert "Briefing type: pre_market" in markdown_path.read_text(encoding="utf-8")


def test_intraday_saves_its_own_json_file(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    _, result = run_pipeline(storage, briefing_type=BriefingType.INTRADAY_10AM)
    assert Path(result.json_path or "").name == "intraday_10am.json"
    assert Path(result.json_path or "").is_file()


def test_json_is_utf8_without_ascii_escaping(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    collector = PlaceholderCollector(clock=lambda: NOW)
    collector.name = "한글수집기"
    pipeline = DailyBriefingPipeline(storage, [collector], clock=lambda: NOW)
    pipeline.run(
        BriefingType.PRE_MARKET,
        TRADING_DATE,
        market_calendar_status="unknown",
        market_calendar_reason="unknown_calendar",
        market_calendar_warning="달력 확인 필요",
    )
    json_path, _ = storage.result_paths(TRADING_DATE, BriefingType.PRE_MARKET)
    text = json_path.read_text(encoding="utf-8")
    assert "한글수집기" in text
    assert "달력 확인 필요" in text
    assert "\\ud55c" not in text.lower()


def test_storage_uses_atomic_replace_and_removes_temp_files(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    with patch("qz_briefing.briefing.storage.os.replace", wraps=__import__("os").replace) as replace:
        run_pipeline(storage)
    assert replace.call_count == 2
    assert not list(tmp_path.rglob("*.tmp"))


def test_second_file_replace_failure_leaves_no_partial_result(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    real_replace = os.replace
    replace_count = 0

    def fail_markdown_replace(source: object, destination: object) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("simulated markdown replace failure")
        real_replace(source, destination)

    with patch(
        "qz_briefing.briefing.storage.os.replace",
        side_effect=fail_markdown_replace,
    ):
        try:
            run_pipeline(storage)
        except OSError as exc:
            assert "simulated markdown replace failure" in str(exc)
        else:
            raise AssertionError("Expected the simulated storage failure")

    json_path, markdown_path = storage.result_paths(
        TRADING_DATE, BriefingType.PRE_MARKET
    )
    assert not json_path.exists()
    assert not markdown_path.exists()
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob("*.bak"))


def test_markdown_replace_failure_restores_existing_result_pair(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    json_path, markdown_path = save_existing_result(storage, status="in_progress")
    original_json = json_path.read_bytes()
    original_markdown = markdown_path.read_bytes()
    real_replace = os.replace

    def fail_new_markdown(source: object, destination: object) -> None:
        source_path = Path(source)  # type: ignore[arg-type]
        if Path(destination) == markdown_path and source_path.suffix == ".tmp":  # type: ignore[arg-type]
            raise OSError("simulated markdown replace failure")
        real_replace(source, destination)

    with patch(
        "qz_briefing.briefing.storage.os.replace", side_effect=fail_new_markdown
    ):
        with pytest.raises(OSError, match="simulated markdown replace failure"):
            run_pipeline(storage)

    assert json_path.read_bytes() == original_json
    assert markdown_path.read_bytes() == original_markdown
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list(tmp_path.rglob("*.bak"))


def test_collectors_run_in_registration_order(tmp_path: Path) -> None:
    calls: list[str] = []
    collectors = [RecordingCollector("one", calls), RecordingCollector("two", calls)]
    run_pipeline(BriefingStorage(tmp_path), collectors)
    assert calls == ["one", "two"]


def test_collector_failure_does_not_stop_later_collectors(tmp_path: Path) -> None:
    calls: list[str] = []
    collectors = [
        RecordingCollector("broken", calls, failure=RuntimeError("failed")),
        RecordingCollector("healthy", calls),
    ]
    storage = BriefingStorage(tmp_path)
    _, result = run_pipeline(storage, collectors)
    saved = load_result(storage, BriefingType.PRE_MARKET)
    assert calls == ["broken", "healthy"]
    assert result.status == "completed_with_errors"
    assert saved["collectors"]["broken"]["error"] == "RuntimeError: failed"
    assert saved["collectors"]["healthy"]["status"] == "success"


def test_all_successful_collectors_produce_completed_status(tmp_path: Path) -> None:
    _, result = run_pipeline(BriefingStorage(tmp_path))
    assert result.status == "completed"


def test_same_process_duplicate_is_skipped_without_collecting(tmp_path: Path) -> None:
    calls: list[str] = []
    collector = RecordingCollector("one", calls)
    storage = BriefingStorage(tmp_path)
    pipeline = DailyBriefingPipeline(storage, [collector], clock=lambda: NOW)
    arguments = {
        "market_calendar_status": "open",
        "market_calendar_reason": "weekday",
    }
    pipeline.run(BriefingType.PRE_MARKET, TRADING_DATE, **arguments)
    json_path, _ = storage.result_paths(TRADING_DATE, BriefingType.PRE_MARKET)
    original = json_path.read_bytes()
    second = pipeline.run(BriefingType.PRE_MARKET, TRADING_DATE, **arguments)
    assert second.status == "skipped"
    assert calls == ["one"]
    assert json_path.read_bytes() == original


def test_storage_failure_allows_same_pipeline_to_retry_collectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    storage = BriefingStorage(tmp_path)
    pipeline = DailyBriefingPipeline(
        storage, [RecordingCollector("one", calls)], clock=lambda: NOW
    )
    real_save = storage.save
    save_attempts = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise OSError("simulated save failure")
        return real_save(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(storage, "save", fail_once)
    arguments = {
        "market_calendar_status": "open",
        "market_calendar_reason": "weekday",
    }
    with pytest.raises(OSError, match="simulated save failure"):
        pipeline.run(BriefingType.PRE_MARKET, TRADING_DATE, **arguments)

    second = pipeline.run(BriefingType.PRE_MARKET, TRADING_DATE, **arguments)
    third = pipeline.run(BriefingType.PRE_MARKET, TRADING_DATE, **arguments)
    assert second.status == "completed"
    assert third.status == "skipped"
    assert calls == ["one", "one"]


def test_existing_completed_json_prevents_reexecution(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    run_pipeline(storage)
    calls: list[str] = []
    second_pipeline = DailyBriefingPipeline(
        storage, [RecordingCollector("second", calls)], clock=lambda: NOW
    )
    result = second_pipeline.run(
        BriefingType.PRE_MARKET,
        TRADING_DATE,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    assert result.status == "skipped"
    assert calls == []


@pytest.mark.parametrize(
    ("overrides", "invalid_field"),
    [
        ({"briefing_type": "intraday_10am"}, "briefing_type"),
        ({"trading_date": "2026-07-20"}, "trading_date"),
        ({"schema_version": 999}, "schema_version"),
    ],
)
def test_invalid_completed_metadata_warns_and_reexecutes(
    tmp_path: Path,
    overrides: dict[str, object],
    invalid_field: str,
) -> None:
    storage = BriefingStorage(tmp_path)
    save_existing_result(storage, **overrides)  # type: ignore[arg-type]
    calls: list[str] = []
    _, result = run_pipeline(storage, [RecordingCollector("one", calls)])
    saved = load_result(storage, BriefingType.PRE_MARKET)
    assert result.status == "completed"
    assert calls == ["one"]
    assert any(invalid_field in warning for warning in saved["warnings"])


def test_json_without_markdown_warns_and_reexecutes(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    _, markdown_path = save_existing_result(storage)
    markdown_path.unlink()
    calls: list[str] = []
    _, result = run_pipeline(storage, [RecordingCollector("one", calls)])
    saved = load_result(storage, BriefingType.PRE_MARKET)
    assert result.status == "completed"
    assert calls == ["one"]
    assert any("incomplete" in warning for warning in saved["warnings"])


def test_valid_json_and_markdown_pair_is_skipped(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    save_existing_result(storage)
    calls: list[str] = []
    _, result = run_pipeline(storage, [RecordingCollector("one", calls)])
    assert result.status == "skipped"
    assert calls == []


def test_corrupt_existing_json_warns_and_reexecutes(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    json_path, _ = storage.result_paths(TRADING_DATE, BriefingType.PRE_MARKET)
    json_path.parent.mkdir(parents=True)
    json_path.write_text("{broken", encoding="utf-8")
    _, result = run_pipeline(storage)
    saved = load_result(storage, BriefingType.PRE_MARKET)
    assert result.status == "completed"
    assert any("could not be read" in warning for warning in saved["warnings"])


def test_intraday_loads_same_day_pre_market_summary(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    run_pipeline(storage)
    run_pipeline(storage, briefing_type=BriefingType.INTRADAY_10AM)
    saved = load_result(storage, BriefingType.INTRADAY_10AM)
    assert saved["pre_market_result"] == {
        "exists": True,
        "status": "completed",
        "completed_at": NOW.isoformat(),
    }


def test_intraday_continues_when_pre_market_is_unavailable(tmp_path: Path) -> None:
    storage = BriefingStorage(tmp_path)
    _, result = run_pipeline(storage, briefing_type=BriefingType.INTRADAY_10AM)
    saved = load_result(storage, BriefingType.INTRADAY_10AM)
    assert result.status == "completed"
    assert saved["pre_market_result"]["exists"] is False
    assert any("Pre-market result unavailable" in item for item in saved["warnings"])


def test_pipeline_saves_analysis_without_changing_collector_payload(
    tmp_path: Path,
) -> None:
    original = {
        "collector": "kiwoom_market_indices",
        "indices": [{"market": "KOSPI", "change_rate": 1.25, "raw": {"등락률": "+1.25"}}],
        "warnings": [],
        "errors": [],
    }

    class AnalysisCollector:
        name = "kiwoom_market_indices"

        def collect(self, context: BriefingContext) -> dict[str, object]:
            return original

    storage = BriefingStorage(tmp_path)
    pipeline = DailyBriefingPipeline(storage, [AnalysisCollector()], clock=lambda: NOW)
    pipeline.run(
        BriefingType.PRE_MARKET,
        TRADING_DATE,
        market_calendar_status="open",
        market_calendar_reason="weekday",
    )
    saved = load_result(storage, BriefingType.PRE_MARKET)
    assert saved["analysis"]["market_state"] == "insufficient_data"
    assert saved["collectors"]["kiwoom_market_indices"]["data"] == original
    assert original["indices"][0]["raw"]["등락률"] == "+1.25"
