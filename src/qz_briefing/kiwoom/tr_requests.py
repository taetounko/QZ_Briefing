# -*- coding: utf-8 -*-
"""Sequential, timeout-bounded read-only Kiwoom TR request queue."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import logging
import time
from typing import Protocol, TypeAlias


class KiwoomTrError(RuntimeError):
    pass


class KiwoomTrTimeoutError(KiwoomTrError):
    pass


class KiwoomTrClosedError(KiwoomTrError):
    pass


# The installed local ENC/legend files do not publish a numeric rate limit.
# Use a conservative, configurable process-wide operating default.
DEFAULT_TR_INTERVAL_MS = 1_000
DEFAULT_OVERLOAD_BACKOFF_MS = (3_000, 7_000, 15_000)
OVERLOAD_ERROR_CODE = -200

LOGGER = logging.getLogger(__name__)


class SignalLike(Protocol):
    def connect(self, callback: Callable[[], None]) -> None: ...


class TimerLike(Protocol):
    timeout: SignalLike

    def setSingleShot(self, single_shot: bool) -> None: ...

    def start(self, milliseconds: int) -> None: ...

    def stop(self) -> None: ...


class EventLoopLike(Protocol):
    def exec_(self) -> int: ...

    def quit(self) -> None: ...


class TrAdapter(Protocol):
    def add_tr_data_listener(self, callback: Callable[..., None]) -> None: ...

    def set_input_value(self, item: str, value: str) -> None: ...

    def request_tr(
        self, request_name: str, tr_code: str, previous_next: int, screen_no: str
    ) -> int: ...

    def get_comm_data(
        self, tr_code: str, request_name: str, index: int, item_name: str
    ) -> str: ...

    def get_repeat_count(self, tr_code: str, request_name: str) -> int: ...


def create_timer() -> TimerLike:
    from PyQt5.QtCore import QTimer

    return QTimer()


def create_event_loop() -> EventLoopLike:
    from PyQt5.QtCore import QEventLoop

    return QEventLoop()


@dataclass(frozen=True)
class TrRequest:
    request_name: str
    tr_code: str
    inputs: Mapping[str, str]
    output_fields: tuple[str, ...]
    timeout_ms: int = 10_000
    repeat: bool = False
    paginate: bool = False
    max_pages: int = 20


TrResult: TypeAlias = dict[str, str] | list[dict[str, str]]


@dataclass
class _QueuedRequest:
    request: TrRequest
    on_success: Callable[[TrResult], None]
    on_error: Callable[[Exception], None]
    screen_no: str | None = None
    timer: TimerLike | None = None
    rows: list[dict[str, str]] | None = None
    page_count: int = 0
    dispatch_timer: TimerLike | None = None
    retry_count: int = 0
    previous_next: int = 0


class ScreenNumberPool:
    """Allocate unique four-digit screens and return them after completion."""

    def __init__(self, first: int = 1000, last: int = 9999) -> None:
        self._available = list(range(last, first - 1, -1))
        self._in_use: set[str] = set()

    def acquire(self) -> str:
        if not self._available:
            raise KiwoomTrError("No TR screen number is available")
        screen_no = f"{self._available.pop():04d}"
        self._in_use.add(screen_no)
        return screen_no

    def release(self, screen_no: str) -> None:
        if screen_no not in self._in_use:
            return
        self._in_use.remove(screen_no)
        # Keep it reusable without immediately colliding with a late response.
        self._available.insert(0, int(screen_no))


class KiwoomTrRequestQueue:
    """Serialize TR calls and correlate replies by request name and screen."""

    def __init__(
        self,
        adapter: TrAdapter,
        *,
        timer_factory: Callable[[], TimerLike] = create_timer,
        event_loop_factory: Callable[[], EventLoopLike] = create_event_loop,
        screen_pool: ScreenNumberPool | None = None,
        minimum_interval_ms: int = DEFAULT_TR_INTERVAL_MS,
        overload_backoff_ms: tuple[int, ...] = DEFAULT_OVERLOAD_BACKOFF_MS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._timer_factory = timer_factory
        self._event_loop_factory = event_loop_factory
        self._screen_pool = screen_pool or ScreenNumberPool()
        self._minimum_interval_ms = max(0, minimum_interval_ms)
        self._overload_backoff_ms = overload_backoff_ms
        self._monotonic = monotonic
        self._last_dispatch_at: float | None = None
        self._pending: deque[_QueuedRequest] = deque()
        self._active: _QueuedRequest | None = None
        self._closed = False
        self._adapter.add_tr_data_listener(self._handle_tr_data)

    @property
    def pending_count(self) -> int:
        return len(self._pending) + int(self._active is not None)

    @property
    def adapter(self) -> TrAdapter:
        return self._adapter

    def submit(
        self,
        request: TrRequest,
        on_success: Callable[[TrResult], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        if self._closed:
            raise KiwoomTrClosedError("TR request queue is closed")
        self._pending.append(_QueuedRequest(request, on_success, on_error))
        estimated_ms = max(0, (self.pending_count - 1) * self._minimum_interval_ms)
        LOGGER.info(
            "TR request queued: pending=%d estimated minimum wait=%dms",
            self.pending_count,
            estimated_ms,
        )
        self._start_next()

    def request(self, request: TrRequest) -> dict[str, str]:
        """Wait in a bounded nested Qt loop while the outer UI keeps processing."""
        event_loop = self._event_loop_factory()
        result: list[TrResult] = []
        errors: list[Exception] = []

        def succeed(data: TrResult) -> None:
            result.append(data)
            event_loop.quit()

        def fail(error: Exception) -> None:
            errors.append(error)
            event_loop.quit()

        self.submit(request, succeed, fail)
        if not result and not errors:
            event_loop.exec_()
        if errors:
            raise errors[0]
        if not result:
            raise KiwoomTrError("TR request ended without a result")
        value = result[0]
        if not isinstance(value, dict):
            raise KiwoomTrError("Single-row TR request returned repeated data")
        return value

    def request_rows(self, request: TrRequest) -> list[dict[str, str]]:
        """Return every repeated output row without changing single-row callers."""
        if not request.repeat:
            raise ValueError("Repeated TR request must set repeat=True")
        event_loop = self._event_loop_factory()
        result: list[TrResult] = []
        errors: list[Exception] = []

        def succeed(data: TrResult) -> None:
            result.append(data)
            event_loop.quit()

        def fail(error: Exception) -> None:
            errors.append(error)
            event_loop.quit()

        self.submit(request, succeed, fail)
        if not result and not errors:
            event_loop.exec_()
        if errors:
            raise errors[0]
        if not result or not isinstance(result[0], list):
            raise KiwoomTrError("Repeated TR request ended without row data")
        return result[0]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        error = KiwoomTrClosedError("TR request queue closed during shutdown")
        active = self._active
        self._active = None
        if active is not None:
            self._cleanup(active)
            active.on_error(error)
        while self._pending:
            self._pending.popleft().on_error(error)

    def stop(self) -> None:
        """Graceful-shutdown compatible alias."""
        self.close()

    def _start_next(self) -> None:
        if self._closed or self._active is not None or not self._pending:
            return
        queued = self._pending.popleft()
        self._active = queued
        try:
            queued.screen_no = self._screen_pool.acquire()
            for item, value in queued.request.inputs.items():
                self._adapter.set_input_value(item, value)
            self._schedule_dispatch(queued, requested_delay_ms=0)
        except Exception as exc:
            self._finish_error(exc)

    def _schedule_dispatch(
        self, queued: _QueuedRequest, *, requested_delay_ms: int
    ) -> None:
        if self._closed or self._active is not queued:
            return
        interval_delay = 0
        if self._last_dispatch_at is not None:
            elapsed_ms = (self._monotonic() - self._last_dispatch_at) * 1_000
            interval_delay = max(0, int(self._minimum_interval_ms - elapsed_ms + 0.999))
        delay_ms = max(requested_delay_ms, interval_delay)
        if delay_ms <= 0:
            self._dispatch(queued)
            return
        timer = self._timer_factory()
        queued.dispatch_timer = timer
        timer.setSingleShot(True)
        timer.timeout.connect(lambda queued=queued: self._dispatch(queued))
        timer.start(delay_ms)

    def _dispatch(self, queued: _QueuedRequest) -> None:
        if self._closed or self._active is not queued:
            return
        if queued.dispatch_timer is not None:
            queued.dispatch_timer.stop()
            queued.dispatch_timer = None
        self._last_dispatch_at = self._monotonic()
        result = self._adapter.request_tr(
            queued.request.request_name,
            queued.request.tr_code,
            queued.previous_next,
            queued.screen_no or "",
        )
        if result == OVERLOAD_ERROR_CODE:
            LOGGER.warning("TR overload detected: %s", queued.request.request_name)
            if queued.retry_count >= len(self._overload_backoff_ms):
                LOGGER.error("TR retry exhausted: %s", queued.request.request_name)
                self._finish_error(KiwoomTrError(
                    f"CommRqData rejected request with result {result} after retries"
                ))
                return
            delay_ms = self._overload_backoff_ms[queued.retry_count]
            queued.retry_count += 1
            LOGGER.warning(
                "TR retry scheduled: %s attempt=%d delay=%dms",
                queued.request.request_name,
                queued.retry_count,
                delay_ms,
            )
            self._schedule_dispatch(queued, requested_delay_ms=delay_ms)
            return
        if result != 0:
            self._finish_error(KiwoomTrError(
                f"CommRqData rejected request with result {result}"
            ))
            return
        timer = queued.timer
        if timer is None:
            timer = self._timer_factory()
            queued.timer = timer
            timer.setSingleShot(True)
            timer.timeout.connect(lambda queued=queued: self._handle_timeout(queued))
        if self._active is queued:
            timer.start(queued.request.timeout_ms)

    def _handle_tr_data(self, *arguments: object) -> None:
        active = self._active
        if active is None or len(arguments) < 3:
            return
        screen_no, request_name, tr_code = map(str, arguments[:3])
        request = active.request
        if (
            screen_no != active.screen_no
            or request_name != request.request_name
            or tr_code.upper() != request.tr_code.upper()
        ):
            return
        try:
            if request.repeat:
                row_count = self._adapter.get_repeat_count(
                    request.tr_code, request.request_name
                )
                data: TrResult = [
                    {
                        field: self._adapter.get_comm_data(
                            request.tr_code, request.request_name, index, field
                        )
                        for field in request.output_fields
                    }
                    for index in range(row_count)
                ]
                if request.paginate:
                    active.rows = (active.rows or []) + data
                    active.page_count += 1
                    previous_next = str(arguments[4]).strip() if len(arguments) > 4 else "0"
                    if previous_next == "2":
                        if active.page_count >= request.max_pages:
                            raise KiwoomTrError(
                                f"TR pagination exceeded {request.max_pages} pages"
                            )
                        if active.timer is not None:
                            active.timer.stop()
                        active.previous_next = 2
                        active.retry_count = 0
                        self._schedule_dispatch(active, requested_delay_ms=0)
                        return
                    data = active.rows
            else:
                data = {
                    field: self._adapter.get_comm_data(
                        request.tr_code, request.request_name, 0, field
                    )
                    for field in request.output_fields
                }
        except Exception as exc:
            self._finish_error(exc)
            return
        self._finish_success(data)

    def _handle_timeout(self, queued: _QueuedRequest) -> None:
        active = self._active
        if active is not queued:
            return
        self._finish_error(
            KiwoomTrTimeoutError(
                f"TR request timed out: {active.request.request_name}"
            )
        )

    def _finish_success(self, data: TrResult) -> None:
        active = self._take_active()
        if active is None:
            return
        try:
            active.on_success(data)
        finally:
            self._start_next()

    def _finish_error(self, error: Exception) -> None:
        active = self._take_active()
        if active is None:
            return
        try:
            active.on_error(error)
        finally:
            self._start_next()

    def _take_active(self) -> _QueuedRequest | None:
        active = self._active
        self._active = None
        if active is not None:
            self._cleanup(active)
        return active

    def _cleanup(self, queued: _QueuedRequest) -> None:
        if queued.timer is not None:
            queued.timer.stop()
        if queued.dispatch_timer is not None:
            queued.dispatch_timer.stop()
        if queued.screen_no is not None:
            self._screen_pool.release(queued.screen_no)
