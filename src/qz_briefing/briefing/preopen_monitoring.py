# -*- coding: utf-8 -*-
"""Qt-friendly pre-open observation using only documented Kiwoom real-time FIDs."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, time, timedelta
from typing import Protocol

WINDOW_START = time(8, 0)
WINDOW_END = time(9, 0)
FINAL_SAMPLE = time(8, 59)
SAMPLE_INTERVAL_SECONDS = 300
REAL_SCREEN_NO = "7300"
# C:\OpenAPI\system\realtime.dat: 주식예상체결 and 장시작시간.
EXPECTED_FIDS = (20, 10, 11, 12, 15, 13, 25)
MARKET_TIME_FIDS = (215, 20, 214)
CORE_CODES = ("005930", "000660")


class TimerLike(Protocol):
    timeout: object
    def setSingleShot(self, value: bool) -> None: ...
    def start(self, milliseconds: int) -> None: ...
    def stop(self) -> None: ...


def sample_times(target: datetime) -> tuple[datetime, ...]:
    """Return local 5-minute boundaries plus the explicit 08:59 final sample."""
    start = datetime.combine(target.date(), WINDOW_START, tzinfo=target.tzinfo)
    end = datetime.combine(target.date(), FINAL_SAMPLE, tzinfo=target.tzinfo)
    values = []
    cursor = start
    while cursor <= end:
        values.append(cursor)
        cursor += timedelta(seconds=SAMPLE_INTERVAL_SECONDS)
    if values[-1] != end:
        values.append(end)
    return tuple(values)


def prioritize_codes(
    holdings: Iterable[str] = (), leaders: Iterable[str] = (), watchlist: Iterable[str] = ()
) -> tuple[str, ...]:
    """Deduplicate targets while retaining the documented operating priority."""
    output: list[str] = []
    for code in (*CORE_CODES, *holdings, *leaders, *watchlist):
        normalized = str(code).strip()
        if normalized and normalized not in output:
            output.append(normalized)
    return tuple(output)


def empty_result(now: datetime, *, actual_start: datetime | None = None) -> dict[str, object]:
    coverage = "not_started" if actual_start is None else (
        "complete" if actual_start.time() <= WINDOW_START else "partial"
    )
    warnings = [] if coverage == "complete" else ["partial_preopen_window" if actual_start else "preopen monitoring was not started"]
    return {
        "window_start": "08:00:00", "window_end": "09:00:00",
        "actual_start": actual_start.isoformat() if actual_start else "",
        "sampling_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "sample_count": 0, "coverage_status": coverage,
        "market_open_detected": False, "indices": {}, "large_caps": {},
        "holdings": [], "leaders": [], "changes": {}, "signals": [],
        "warnings": warnings,
        "flow_availability": {
            "foreign_flow": "not_available", "institution_flow": "not_available",
            "reason": "official pre-open investor-flow data is not available from the confirmed interface",
        },
    }


class PreopenMonitoringController:
    """Capture bounded summaries at absolute local times without sleeping."""
    def __init__(self, source: Callable[[], dict[str, object]], *, clock: Callable[[], datetime], timer_factory: Callable[[], TimerLike]) -> None:
        self._source, self._clock, self._timer_factory = source, clock, timer_factory
        self._timer: TimerLike | None = None
        self._pending: list[datetime] = []
        self._stopped = False
        self._result = empty_result(clock())

    @property
    def result(self) -> dict[str, object]:
        return dict(self._result)

    def start(self) -> None:
        if self._stopped or self._result["actual_start"]:
            return
        now = self._clock()
        self._result = empty_result(now, actual_start=now)
        self._pending = [value for value in sample_times(now) if value >= now]
        if now.time() >= WINDOW_START and now.time() < WINDOW_END:
            self._capture()
        self._schedule_next()

    def stop(self) -> None:
        self._stopped = True
        self._pending.clear()
        if self._timer is not None:
            self._timer.stop()

    def refresh_market_state(self) -> bool:
        """Refresh only the open flag; this does not add a sampling observation."""
        try:
            latest = self._source()
            if latest.get("market_open_detected"):
                self._result["market_open_detected"] = True
        except Exception as exc:
            warnings = self._result["warnings"]
            assert isinstance(warnings, list)
            warnings.append(f"market state unavailable: {type(exc).__name__}: {exc}")
        return bool(self._result["market_open_detected"])

    def _schedule_next(self) -> None:
        if self._stopped or not self._pending:
            return
        target = self._pending.pop(0)
        delay = max(0, int((target - self._clock()).total_seconds() * 1000))
        timer = self._timer_factory()
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_timeout)  # type: ignore[attr-defined]
        timer.start(delay)
        self._timer = timer

    def _on_timeout(self) -> None:
        if not self._stopped:
            self._capture()
            self._schedule_next()

    def _capture(self) -> None:
        try:
            sample = self._source()
            samples = self._result.setdefault("samples", [])
            assert isinstance(samples, list)
            samples.append({"collected_at": self._clock().isoformat(), **sample})
            self._result["sample_count"] = len(samples)
            if sample.get("market_open_detected"):
                self._result["market_open_detected"] = True
        except Exception as exc:
            warnings = self._result["warnings"]
            assert isinstance(warnings, list)
            warnings.append(f"preopen sample unavailable: {type(exc).__name__}: {exc}")


class KiwoomPreopenRealSource:
    """Maintain the latest official real-time fields; never labels them as investor flow."""
    def __init__(self, adapter: object, codes: Iterable[str]) -> None:
        self._adapter = adapter
        self._codes = list(dict.fromkeys(str(code).strip() for code in codes if str(code).strip()))
        self._latest: dict[str, dict[str, str]] = {}
        self._market_state = ""
        self._first_trade_detected = False
        self._started = False
        adapter.add_real_data_listener(self._on_real_data)

    def start(self) -> None:
        # SetRealReg is documented in koa_devguide.xml. 장시작시간 is delivered
        # independently by the server; stock registrations use expected-price FIDs.
        result = self._adapter.register_real_data(REAL_SCREEN_NO, self._codes, EXPECTED_FIDS)
        if result != 0:
            raise RuntimeError(f"SetRealReg failed with result {result}")
        self._started = True

    def snapshot(self) -> dict[str, object]:
        return {
            "large_caps": {code: dict(values) for code, values in self._latest.items()},
            "market_state": self._market_state or "not_available",
            "market_open_detected": self._market_state in {"3", "R"} or self._first_trade_detected,
            "data_source": "Kiwoom OpenAPI+ real-time 주식예상체결/장시작시간",
        }

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._adapter.unregister_real_data(REAL_SCREEN_NO)

    def _on_real_data(self, code: str, real_type: str, raw: str) -> None:
        del raw
        if real_type == "장시작시간":
            self._market_state = self._adapter.get_comm_real_data(code, 215)
            return
        if real_type == "주식체결":
            self._first_trade_detected = True
            return
        if real_type != "주식예상체결" or code not in self._codes:
            return
        names = {20: "time", 10: "expected_price", 11: "change", 12: "change_rate", 15: "expected_volume", 13: "accumulated_volume", 25: "change_sign"}
        self._latest[code] = {
            name: self._adapter.get_comm_real_data(code, fid) for fid, name in names.items()
        }
