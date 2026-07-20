"""Tests for pure briefing plans and the timer adapter."""

from datetime import datetime

from qz_briefing.scheduling.briefing_scheduler import (
    INTRADAY_10AM,
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


def test_before_8_am_schedules_pre_market_and_10_am() -> None:
    plan = briefing_plan(datetime(2026, 7, 20, 7, 30))
    assert [(item.name, item.run_immediately, item.delay_ms) for item in plan] == [
        (PRE_MARKET, False, 30 * 60 * 1000),
        (INTRADAY_10AM, False, 150 * 60 * 1000),
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


def test_at_or_after_10_runs_only_intraday_immediately() -> None:
    at_ten = briefing_plan(datetime(2026, 7, 20, 10, 0))
    after_ten = briefing_plan(datetime(2026, 7, 20, 15, 0))
    assert [(item.name, item.run_immediately) for item in at_ten] == [
        (INTRADAY_10AM, True)
    ]
    assert [(item.name, item.run_immediately) for item in after_ten] == [
        (INTRADAY_10AM, True)
    ]


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

    scheduler = BriefingScheduler(timer_factory=make_timer)
    scheduler.schedule(datetime(2026, 7, 20, 7, 30))
    scheduler.stop()
    scheduler.stop()

    assert len(timers) == 2
    assert [timer.stop_count for timer in timers] == [1, 1]


def test_default_placeholder_logs_intraday_task_name(capsys: object) -> None:
    scheduler = BriefingScheduler()

    scheduler.schedule(datetime(2026, 7, 20, 10, 0))

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "briefing task placeholder: intraday_10am" in output
