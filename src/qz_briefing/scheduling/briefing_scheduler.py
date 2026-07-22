"""Pure briefing-time policy plus a small Qt timer adapter."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Protocol

from qz_briefing.briefing.models import BriefingType

PRE_MARKET = BriefingType.PRE_MARKET.value
INTRADAY_10AM = BriefingType.INTRADAY_10AM.value
MARKET_CLOSE = BriefingType.MARKET_CLOSE.value
PRE_MARKET_TIME = time(8, 0)
INTRADAY_TIME = time(10, 0)
MARKET_CLOSE_TIME = time(15, 40)
OPERATING_END_TIME = time(20, 0)


@dataclass(frozen=True)
class BriefingPlanItem:
    name: str
    run_immediately: bool
    delay_ms: int


def briefing_plan(now: datetime) -> tuple[BriefingPlanItem, ...]:
    """Return today's explicit catch-up and future scheduling policy."""
    pre_market_at = datetime.combine(now.date(), PRE_MARKET_TIME, tzinfo=now.tzinfo)
    intraday_at = datetime.combine(now.date(), INTRADAY_TIME, tzinfo=now.tzinfo)
    market_close_at = datetime.combine(now.date(), MARKET_CLOSE_TIME, tzinfo=now.tzinfo)
    operating_end_at = datetime.combine(now.date(), OPERATING_END_TIME, tzinfo=now.tzinfo)

    def scheduled(name: str, at: datetime) -> BriefingPlanItem:
        return BriefingPlanItem(name, False, math.ceil((at - now).total_seconds() * 1000))

    if now >= operating_end_at:
        return ()

    if now < pre_market_at:
        return (
            scheduled(PRE_MARKET, pre_market_at),
            scheduled(INTRADAY_10AM, intraday_at),
            scheduled(MARKET_CLOSE, market_close_at),
        )
    if now < intraday_at:
        return (
            BriefingPlanItem(PRE_MARKET, True, 0),
            scheduled(INTRADAY_10AM, intraday_at),
            scheduled(MARKET_CLOSE, market_close_at),
        )
    if now < market_close_at:
        return (
            BriefingPlanItem(INTRADAY_10AM, True, 0),
            scheduled(MARKET_CLOSE, market_close_at),
        )
    return (BriefingPlanItem(MARKET_CLOSE, True, 0),)


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


class BriefingScheduler:
    """Execute each named briefing callback at most once per local date."""

    def __init__(
        self,
        callbacks: Mapping[str, BriefingCallback],
        *,
        timer_factory: TimerFactory = create_timer,
    ) -> None:
        self._callbacks = dict(callbacks)
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
