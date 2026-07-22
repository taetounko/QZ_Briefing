# -*- coding: utf-8 -*-
"""Unattended-operation safeguards without account or trading data."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import tempfile
import time as time_module
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
BRIEFING_DEADLINES = {
    "pre_market": (time(9, 0), timedelta(minutes=10)),
    "intraday_10am": (time(10, 0), timedelta(minutes=10)),
    "market_close": (time(15, 40), timedelta(minutes=15)),
}
VALID_COMPLETION = {"completed", "completed_with_errors", "no_market_open"}
SENSITIVE_KEYWORDS = {"account", "account_id", "quantity", "average_price", "profit", "holdings", "password", "certificate"}


class TimerLike(Protocol):
    timeout: object
    def start(self, milliseconds: int) -> None: ...
    def stop(self) -> None: ...


def atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class SleepInhibitor:
    """Idempotent SetThreadExecutionState owner; display sleep is untouched."""
    def __init__(self, *, enabled: bool = True, api: Callable[[int], int] | None = None) -> None:
        self.enabled, self.active = enabled, False
        self._api = api or self._windows_api
        self.warnings: list[str] = []

    @staticmethod
    def _windows_api(flags: int) -> int:
        if os.name != "nt": return 1
        return int(ctypes.windll.kernel32.SetThreadExecutionState(flags))

    def start(self) -> bool:
        if not self.enabled or self.active: return self.active
        try:
            if not self._api(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                raise OSError("SetThreadExecutionState returned zero")
            self.active = True; return True
        except Exception as exc:
            self.warnings.append(f"system sleep prevention unavailable: {type(exc).__name__}: {exc}")
            logging.getLogger(__name__).warning(self.warnings[-1]); return False

    def stop(self) -> None:
        if not self.active: return
        try: self._api(ES_CONTINUOUS)
        except Exception as exc: self.warnings.append(f"system sleep prevention release failed: {exc}")
        finally: self.active = False


def briefing_result_status(root: Path, target: date, name: str) -> str | None:
    path = root / "briefings" / f"{target.year:04d}" / f"{target.month:02d}" / f"{target.day:02d}" / f"{name}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return None
    if not isinstance(value, dict) or value.get("briefing_type") != name: return None
    return str(value.get("status")) if value.get("status") in VALID_COMPLETION else None


class MissingBriefingRecovery:
    def __init__(self, root: Path, callbacks: Mapping[str, Callable[[], None]], *, clock: Callable[[], datetime] = datetime.now, running: Callable[[str], bool] = lambda name: False, pending: Callable[[str], bool] = lambda name: False) -> None:
        self.root, self.callbacks, self.clock = Path(root), dict(callbacks), clock
        self.running, self.pending = running, pending
        self.recovered: set[tuple[date, str]] = set()

    def check(self) -> list[str]:
        now = self.clock(); recovered = []
        if now.time() >= time(20): return recovered
        for name, (scheduled, grace) in BRIEFING_DEADLINES.items():
            deadline = datetime.combine(now.date(), scheduled, tzinfo=now.tzinfo) + grace
            key = (now.date(), name)
            if now < deadline or key in self.recovered: continue
            if briefing_result_status(self.root, now.date(), name) or self.running(name) or self.pending(name): continue
            callback = self.callbacks.get(name)
            if callback:
                self.recovered.add(key); callback(); recovered.append(name)
        return recovered


def runtime_health(connection: str, *, active_briefing: str | None, delayed: bool, tr_stalled: bool, shutting_down: bool, errors: int = 0) -> str:
    if shutting_down: return "shutting_down"
    if errors >= 3: return "failed"
    if tr_stalled: return "tr_stalled"
    if active_briefing: return "briefing_delayed" if delayed else "briefing_running"
    if connection in {"RECONNECT_WAIT", "RECONNECTING", "RECHECKING"}: return "reconnecting"
    if connection != "CONNECTED": return "waiting_for_login"
    return "degraded" if errors else "healthy"


class RuntimeMonitor:
    """Atomic heartbeat, stale marker detection, bounded recovery and daily summary."""
    def __init__(self, root: Path, *, timer_factory: Callable[[], TimerLike], clock: Callable[[], datetime] = datetime.now, monotonic: Callable[[], float] = time_module.monotonic, pid: int | None = None, connection_state: Callable[[], str] = lambda: "DISCONNECTED", tr_progress: Callable[[], Mapping[str, object]] = dict, watchdog_recover: Callable[[str], None] | None = None) -> None:
        self.root, self.clock = Path(root), clock
        self.pid, self.started_at = pid or os.getpid(), clock()
        self.connection_state, self.tr_progress = connection_state, tr_progress
        self.monotonic, self.watchdog_recover = monotonic, watchdog_recover
        self.timer = timer_factory(); self.timer.timeout.connect(self.beat)
        self.active_briefing: str | None = None; self.last_completed_briefing: str | None = None
        self.active_briefing_started_at: datetime | None = None
        self._watchdog_handled = False
        self.next_scheduled_task: str | None = None; self.next_scheduled_at: str | None = None
        self.warnings: list[str] = []; self.errors: list[str] = []; self.events: list[dict[str, str]] = []
        self.recovery_count = 0; self.connection_drop_count = 0; self.reconnect_count = 0
        self.timeout_count = 0; self.overload_retry_count = 0; self.shutting_down = False
        self.stale_previous = self._read_stale_marker()
        self._cleanup_runtime_temps()
        self.recovery: MissingBriefingRecovery | None = None
        self.extra_status: Callable[[], Mapping[str, object]] = dict
        self.summary_listener: Callable[[dict[str, object]], None] | None = None

    @property
    def runtime_dir(self): return self.root / "runtime"
    def start(self) -> None:
        self._write_marker(); self.beat(); self.timer.start(60_000)
    def beat(self) -> None:
        if self.recovery is not None:
            recovered = self.recovery.check()
            self.recovery_count += len(recovered)
            for name in recovered: self.event(f"briefing recovery requested: {name}")
        progress = dict(self.tr_progress() or {})
        stalled = self.tr_stalled(progress) or bool(
            self.active_briefing_started_at
            and self.clock() - self.active_briefing_started_at >= timedelta(seconds=300)
        )
        if stalled and not self._watchdog_handled and not self.shutting_down:
            self._watchdog_handled = True; self.warnings.append("runtime watchdog detected stalled progress")
            if self.watchdog_recover is not None: self.watchdog_recover("runtime watchdog detected stalled progress")
        payload = {"pid": self.pid, "started_at": self.started_at.isoformat(), "last_heartbeat_at": self.clock().isoformat(), "connection_state": self.connection_state(), "runtime_state": "shutting_down" if self.shutting_down else "waiting", "active_briefing": self.active_briefing, "last_completed_briefing": self.last_completed_briefing, "next_scheduled_task": self.next_scheduled_task, "next_scheduled_at": self.next_scheduled_at, "shutdown_scheduled_at": "20:00:00", "health": runtime_health(self.connection_state(), active_briefing=self.active_briefing, delayed=stalled, tr_stalled=stalled, shutting_down=self.shutting_down, errors=len(self.errors)), "warnings": list(self.warnings)}
        payload.update(dict(self.extra_status() or {}))
        atomic_write_json(self.runtime_dir / "heartbeat.json", payload); self._write_marker()
    def tr_stalled(self, progress: Mapping[str, object]) -> bool:
        active = progress.get("active_request")
        started = progress.get("last_request_started_at")
        if progress.get("consecutive_timeouts", 0): return False
        return bool(active and isinstance(started, (int, float)) and self.monotonic() - float(started) >= 60)
    def briefing_started(self, name: str) -> None: self.active_briefing = name; self.active_briefing_started_at = self.clock(); self._watchdog_handled = False; self.event(f"briefing started: {name}")
    def briefing_completed(self, name: str) -> None: self.active_briefing = None; self.active_briefing_started_at = None; self._watchdog_handled = False; self.last_completed_briefing = name; self.event(f"briefing completed: {name}"); self.beat()
    def event(self, message: str) -> None:
        self.events.append({"at": self.clock().isoformat(), "message": message}); self.events = self.events[-20:]
    def stop(self) -> None:
        if self.shutting_down: return
        self.shutting_down = True; self.timer.stop()
        try: self.beat()
        finally:
            self._write_summary(normal_shutdown=True)
            try: (self.runtime_dir / "running.json").unlink()
            except FileNotFoundError: pass
    def _write_marker(self) -> None:
        atomic_write_json(self.runtime_dir / "running.json", {"pid": self.pid, "started_at": self.started_at.isoformat(), "last_heartbeat_at": self.clock().isoformat(), "active_briefing": self.active_briefing, "connection_state": self.connection_state()})
    def _cleanup_runtime_temps(self) -> None:
        if not self.runtime_dir.exists(): return
        for path in self.runtime_dir.glob(".*.tmp"):
            try: path.unlink()
            except OSError: pass
    def _read_stale_marker(self):
        path = self.runtime_dir / "running.json"
        try: value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError): return None
        if not isinstance(value, dict): return None
        pid = value.get("pid")
        if isinstance(pid, int) and _pid_alive(pid): return {"alive": True, **value}
        return {"alive": False, **value}
    def _write_summary(self, *, normal_shutdown: bool) -> None:
        results = {name: briefing_result_status(self.root, self.clock().date(), name) for name in BRIEFING_DEADLINES}
        completed = sum(value is not None for value in results.values())
        overall = "successful" if completed == 3 and not self.warnings and not self.errors else "successful_with_warnings" if completed == 3 else "partial" if completed else "failed"
        payload = {"started_at": self.started_at.isoformat(), "automatic_start": True, "automatic_login_result": self.connection_state(), "briefings": results, "connection_drop_count": self.connection_drop_count, "automatic_reconnect_count": self.reconnect_count, "tr_timeout_count": self.timeout_count, "overload_retry_count": self.overload_retry_count, "briefing_recovery_count": self.recovery_count, "warning_count": len(self.warnings), "error_count": len(self.errors), "normal_shutdown": normal_shutdown, "ended_at": self.clock().isoformat(), "overall_result": overall, "events": self.events[-20:]}
        target = self.runtime_dir / "history" / f"{self.clock().date().isoformat()}.json"; atomic_write_json(target, payload)
        if self.summary_listener is not None:
            try: self.summary_listener(payload)
            except Exception: logging.getLogger(__name__).exception("daily summary notification enqueue failed")


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid(): return True
    if os.name != "nt": return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle: ctypes.windll.kernel32.CloseHandle(handle); return True
    return False


class SensitiveDataFilter(logging.Filter):
    ACCOUNT_PATTERN = re.compile(r"(?<!\d)\d{8,12}(?!\d)")
    def filter(self, record: logging.LogRecord) -> bool:
        message = self.ACCOUNT_PATTERN.sub(lambda match: "*" * (len(match.group()) - 4) + match.group()[-4:], record.getMessage())
        record.msg, record.args = message, (); return True


def configure_daily_logging(root: Path, *, today: date | None = None, retention_days: int = 30) -> logging.Handler | None:
    log_dir = Path(root) / "logs"; log_dir.mkdir(parents=True, exist_ok=True); today = today or date.today()
    logger = logging.getLogger()
    marker = str(log_dir.resolve())
    for handler in logger.handlers:
        if getattr(handler, "_qz_daily_log", None) == marker: return handler
    try:
        handler = logging.FileHandler(log_dir / f"qz_briefing_{today.isoformat()}.log", encoding="utf-8")
        handler._qz_daily_log = marker; handler.addFilter(SensitiveDataFilter()); logger.addHandler(handler)
    except OSError:
        logging.getLogger(__name__).exception("daily log file unavailable"); return None
    cutoff = today - timedelta(days=retention_days)
    for path in log_dir.glob("qz_briefing_????-??-??.log"):
        try:
            stamp = date.fromisoformat(path.stem.removeprefix("qz_briefing_"))
            if stamp < cutoff: path.unlink()
        except (ValueError, OSError): pass
    return handler
