# -*- coding: utf-8 -*-

import json
import logging
from datetime import date, datetime, timedelta

from qz_briefing.runtime.unattended import (
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, MissingBriefingRecovery, RuntimeMonitor,
    SensitiveDataFilter, SleepInhibitor, atomic_write_json,
    briefing_result_status, configure_daily_logging, runtime_health,
)


class Signal:
    def __init__(self): self.callback = None
    def connect(self, callback): self.callback = callback


class Timer:
    def __init__(self): self.timeout, self.started, self.stopped = Signal(), [], 0
    def start(self, value): self.started.append(value)
    def stop(self): self.stopped += 1


def test_sleep_inhibitor_is_idempotent_and_releases():
    calls = []
    guard = SleepInhibitor(api=lambda flags: calls.append(flags) or 1)
    assert guard.start(); assert guard.start(); guard.stop(); guard.stop()
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]


def test_sleep_api_failure_warns_and_continues():
    guard = SleepInhibitor(api=lambda flags: 0)
    assert not guard.start() and guard.warnings


def test_atomic_heartbeat_has_no_sensitive_details_and_stops(tmp_path):
    timer = Timer(); now = datetime(2026, 7, 22, 7, 30)
    monitor = RuntimeMonitor(tmp_path, timer_factory=lambda: timer, clock=lambda: now, pid=123, connection_state=lambda: "CONNECTED")
    monitor.start()
    heartbeat = json.loads((tmp_path / "runtime" / "heartbeat.json").read_text(encoding="utf-8"))
    assert timer.started == [60_000]
    assert heartbeat["health"] == "healthy"
    text = json.dumps(heartbeat)
    assert "account" not in text and "profit" not in text and "holdings" not in text
    monitor.stop(); assert timer.stopped == 1
    assert not (tmp_path / "runtime" / "running.json").exists()


def _write_result(root, now, name, status="completed"):
    path = root / "briefings" / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}" / f"{name}.json"
    atomic_write_json(path, {"briefing_type": name, "status": status})


def test_missing_briefing_recovers_once_after_grace(tmp_path):
    calls = []
    now = datetime(2026, 7, 22, 9, 10)
    recovery = MissingBriefingRecovery(tmp_path, {"pre_market": lambda: calls.append("pre_market")}, clock=lambda: now)
    assert recovery.check() == ["pre_market"]
    assert recovery.check() == [] and calls == ["pre_market"]


def test_missing_recovery_respects_running_pending_close_and_20h(tmp_path):
    calls = []
    clock_value = [datetime(2026, 7, 22, 10, 10)]
    recovery = MissingBriefingRecovery(tmp_path, {"intraday_10am": lambda: calls.append(1)}, clock=lambda: clock_value[0], running=lambda name: True)
    assert recovery.check() == []
    recovery.running = lambda name: False; recovery.pending = lambda name: True
    assert recovery.check() == []
    recovery.pending = lambda name: False; _write_result(tmp_path, clock_value[0].date(), "intraday_10am", "no_market_open")
    assert recovery.check() == []
    clock_value[0] = datetime(2026, 7, 22, 20, 0); assert recovery.check() == []


def test_validation_does_not_count_as_regular_completion(tmp_path):
    day = date(2026, 7, 22)
    _write_result(tmp_path, day, "market_close_validation")
    assert briefing_result_status(tmp_path, day, "market_close") is None


def test_runtime_health_states():
    assert runtime_health("CONNECTED", active_briefing=None, delayed=False, tr_stalled=False, shutting_down=False) == "healthy"
    assert runtime_health("DISCONNECTED", active_briefing=None, delayed=False, tr_stalled=False, shutting_down=False) == "waiting_for_login"
    assert runtime_health("CONNECTED", active_briefing="pre", delayed=True, tr_stalled=False, shutting_down=False) == "briefing_delayed"
    assert runtime_health("CONNECTED", active_briefing=None, delayed=False, tr_stalled=True, shutting_down=False) == "tr_stalled"
    assert runtime_health("CONNECTED", active_briefing=None, delayed=False, tr_stalled=False, shutting_down=True) == "shutting_down"


def test_watchdog_recovers_once_and_does_not_duplicate_timeout(tmp_path):
    recovery = []; progress = {"active_request": "OPT", "last_request_started_at": 1.0, "consecutive_timeouts": 0}
    monitor = RuntimeMonitor(tmp_path, timer_factory=Timer, monotonic=lambda: 62.0, tr_progress=lambda: progress, watchdog_recover=recovery.append)
    monitor.beat(); monitor.beat()
    assert len(recovery) == 1
    progress["consecutive_timeouts"] = 1
    other = RuntimeMonitor(tmp_path / "other", timer_factory=Timer, monotonic=lambda: 62.0, tr_progress=lambda: progress, watchdog_recover=recovery.append)
    other.beat(); assert len(recovery) == 1


def test_stale_marker_is_observed_and_replaced(tmp_path):
    atomic_write_json(tmp_path / "runtime" / "running.json", {"pid": 999999, "active_briefing": "pre_market"})
    monitor = RuntimeMonitor(tmp_path, timer_factory=Timer, pid=321)
    assert monitor.stale_previous["alive"] is False


def test_log_masking_retention_and_handler_deduplication(tmp_path):
    old = tmp_path / "logs" / "qz_briefing_2026-06-01.log"; old.parent.mkdir(); old.write_text("old")
    recent = tmp_path / "logs" / "qz_briefing_2026-07-01.log"; recent.write_text("recent")
    first = configure_daily_logging(tmp_path, today=date(2026, 7, 22)); second = configure_daily_logging(tmp_path, today=date(2026, 7, 22))
    assert first is second and not old.exists() and recent.exists()
    record = logging.LogRecord("x", logging.INFO, "", 0, "account 1234567890", (), None)
    SensitiveDataFilter().filter(record)
    assert "1234567890" not in record.getMessage() and record.getMessage().endswith("7890")
    logging.getLogger().removeHandler(first); first.close()


def test_daily_summary_excludes_account_and_profit_details(tmp_path):
    now = datetime(2026, 7, 22, 20)
    monitor = RuntimeMonitor(tmp_path, timer_factory=Timer, clock=lambda: now, pid=2, connection_state=lambda: "CONNECTED")
    monitor.start(); monitor.stop()
    payload = json.loads((tmp_path / "runtime" / "history" / "2026-07-22.json").read_text(encoding="utf-8"))
    text = json.dumps(payload)
    assert "account" not in text and "holdings" not in text and "profit_loss" not in text
