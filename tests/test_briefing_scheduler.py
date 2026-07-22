"""Tests for pure briefing plans and the timer adapter."""

from datetime import datetime

from qz_briefing.scheduling.briefing_scheduler import (
    INTRADAY_10AM,
    MARKET_CLOSE,
    PRE_MARKET,
    BriefingScheduler,
    briefing_plan,
)


class FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in tuple(self.callbacks):
            callback()  # type: ignore[operator]


class FakeTimer:
    def __init__(self) -> None:
        self.timeout = FakeSignal()
        self.single_shot = False
        self.delay_ms: int | None = None
        self.stop_count = 0

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, milliseconds: int) -> None:
        self.delay_ms = milliseconds

    def stop(self) -> None:
        self.stop_count += 1


def test_before_8_am_schedules_pre_market_10_am_and_market_close() -> None:
    plan = briefing_plan(datetime(2026, 7, 20, 7, 30))
    assert [(item.name, item.run_immediately, item.delay_ms) for item in plan] == [
        (PRE_MARKET, False, 30 * 60 * 1000),
        (INTRADAY_10AM, False, 150 * 60 * 1000),
        (MARKET_CLOSE, False, 490 * 60 * 1000),
    ]


def test_at_8_am_runs_pre_market_now_and_schedules_10_am() -> None:
    plan = briefing_plan(datetime(2026, 7, 20, 8, 0))
    assert plan[0].name == PRE_MARKET
    assert plan[0].run_immediately
    assert plan[1].name == INTRADAY_10AM
    assert not plan[1].run_immediately
    assert plan[1].delay_ms == 2 * 60 * 60 * 1000


def test_between_8_and_10_runs_pre_market_immediately() -> None:
    plan = briefing_plan(datetime(2026, 7, 20, 9, 59, 59))
    assert plan[0].name == PRE_MARKET
    assert plan[0].run_immediately
    assert plan[1].delay_ms == 1000


def test_at_10_runs_intraday_and_schedules_market_close() -> None:
    at_ten = briefing_plan(datetime(2026, 7, 20, 10, 0))
    assert [(item.name, item.run_immediately) for item in at_ten] == [
        (INTRADAY_10AM, True), (MARKET_CLOSE, False)
    ]


def test_market_close_time_policy_and_operating_end() -> None:
    before = briefing_plan(datetime(2026, 7, 20, 15, 39, 59))
    at_close = briefing_plan(datetime(2026, 7, 20, 15, 40))
    after_close = briefing_plan(datetime(2026, 7, 20, 19, 59))
    at_end = briefing_plan(datetime(2026, 7, 20, 20, 0))
    assert before[-1].name == MARKET_CLOSE and before[-1].delay_ms == 1000
    assert [(item.name, item.run_immediately) for item in at_close] == [(MARKET_CLOSE, True)]
    assert [(item.name, item.run_immediately) for item in after_close] == [(MARKET_CLOSE, True)]
    assert at_end == ()


def test_scheduler_prevents_duplicate_execution_for_same_day_and_name() -> None:
    calls: list[str] = []
    timers: list[FakeTimer] = []

    def make_timer() -> FakeTimer:
        timer = FakeTimer()
        timers.append(timer)
        return timer

    scheduler = BriefingScheduler(
        {
            PRE_MARKET: lambda: calls.append(PRE_MARKET),
            INTRADAY_10AM: lambda: calls.append(INTRADAY_10AM),
        },
        timer_factory=make_timer,
    )
    scheduler.schedule(datetime(2026, 7, 20, 7, 30))

    timers[0].timeout.emit()
    timers[0].timeout.emit()

    assert calls == [PRE_MARKET]


def test_stop_cancels_all_scheduled_timers() -> None:
    timers: list[FakeTimer] = []

    def make_timer() -> FakeTimer:
        timer = FakeTimer()
        timers.append(timer)
        return timer

    scheduler = BriefingScheduler(
        {PRE_MARKET: lambda: None, INTRADAY_10AM: lambda: None, MARKET_CLOSE: lambda: None},
        timer_factory=make_timer,
    )
    scheduler.schedule(datetime(2026, 7, 20, 7, 30))
    scheduler.stop()
    scheduler.stop()

    assert len(timers) == 3
    assert [timer.stop_count for timer in timers] == [1, 1, 1]


def test_injected_callback_runs_for_intraday_task() -> None:
    calls: list[str] = []
    scheduler = BriefingScheduler(
        {
            PRE_MARKET: lambda: calls.append(PRE_MARKET),
            INTRADAY_10AM: lambda: calls.append(INTRADAY_10AM),
        }
    )

    scheduler.schedule(datetime(2026, 7, 20, 10, 0))

    assert calls == [INTRADAY_10AM]
