"""Pure briefing-time policy plus a small Qt timer adapter."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol


PRE_MARKET = "pre_market"
INTRADAY_10AM = "intraday_10am"
PRE_MARKET_TIME = time(8, 0)
INTRADAY_TIME = time(10, 0)


@dataclass(frozen=True)
class BriefingPlanItem:
    name: str
    run_immediately: bool
    delay_ms: int


def briefing_plan(now: datetime) -> tuple[BriefingPlanItem, ...]:
    """Return today's explicit catch-up and future scheduling policy."""
    pre_market_at = datetime.combine(now.date(), PRE_MARKET_TIME, tzinfo=now.tzinfo)
    intraday_at = datetime.combine(now.date(), INTRADAY_TIME, tzinfo=now.tzinfo)

    if now < pre_market_at:
        return (
            BriefingPlanItem(
                PRE_MARKET,
                False,
                math.ceil((pre_market_at - now).total_seconds() * 1000),
            ),
            BriefingPlanItem(
                INTRADAY_10AM,
                False,
                math.ceil((intraday_at - now).total_seconds() * 1000),
            ),
        )
    if now < intraday_at:
        return (
            BriefingPlanItem(PRE_MARKET, True, 0),
            BriefingPlanItem(
                INTRADAY_10AM,
                False,
                math.ceil((intraday_at - now).total_seconds() * 1000),
            ),
        )

    # Explicit late-start policy: skip stale pre-market work at/after 10:00.
    return (BriefingPlanItem(INTRADAY_10AM, True, 0),)


class SignalLike(Protocol):
    def connect(self, callback: Callable[[], None]) -> None: ...


class TimerLike(Protocol):
    timeout: SignalLike

    def setSingleShot(self, single_shot: bool) -> None: ...

    def start(self, milliseconds: int) -> None: ...

    def stop(self) -> None: ...


TimerFactory = Callable[[], TimerLike]
BriefingCallback = Callable[[], None]


def create_timer() -> TimerLike:
    from PyQt5.QtCore import QTimer

    return QTimer()


def placeholder(name: str) -> BriefingCallback:
    def run_placeholder() -> None:
        print(f"briefing task placeholder: {name}", flush=True)

    return run_placeholder


class BriefingScheduler:
    """Execute each named briefing callback at most once per local date."""

    def __init__(
        self,
        callbacks: Mapping[str, BriefingCallback] | None = None,
        *,
        timer_factory: TimerFactory = create_timer,
    ) -> None:
        self._callbacks = dict(
            callbacks
            or {
                PRE_MARKET: placeholder(PRE_MARKET),
                INTRADAY_10AM: placeholder(INTRADAY_10AM),
            }
        )
        self._timer_factory = timer_factory
        self._timers: list[TimerLike] = []
        self._executed: set[tuple[date, str]] = set()
        self._stopped = False

    def schedule(self, now: datetime) -> tuple[BriefingPlanItem, ...]:
        plan = briefing_plan(now)
        for item in plan:
            if item.run_immediately:
                self._execute_once(now.date(), item.name)
                continue
            timer = self._timer_factory()
            timer.setSingleShot(True)
            timer.timeout.connect(
                lambda target_date=now.date(), name=item.name: self._execute_once(
                    target_date, name
                )
            )
            timer.start(item.delay_ms)
            self._timers.append(timer)
            print(
                f"briefing task scheduled: {item.name} in {item.delay_ms} ms",
                flush=True,
            )
        return plan

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for timer in self._timers:
            timer.stop()

    def _execute_once(self, target_date: date, name: str) -> bool:
        key = (target_date, name)
        if self._stopped or key in self._executed:
            return False
        self._executed.add(key)
        self._callbacks[name]()
        return True
