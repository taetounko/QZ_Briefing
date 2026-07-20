# -*- coding: utf-8 -*-
"""Sequential, timeout-bounded read-only Kiwoom TR request queue."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol


class KiwoomTrError(RuntimeError):
    pass


class KiwoomTrTimeoutError(KiwoomTrError):
    pass


class KiwoomTrClosedError(KiwoomTrError):
    pass


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


@dataclass
class _QueuedRequest:
    request: TrRequest
    on_success: Callable[[dict[str, str]], None]
    on_error: Callable[[Exception], None]
    screen_no: str | None = None
    timer: TimerLike | None = None


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
    ) -> None:
        self._adapter = adapter
        self._timer_factory = timer_factory
        self._event_loop_factory = event_loop_factory
        self._screen_pool = screen_pool or ScreenNumberPool()
        self._pending: deque[_QueuedRequest] = deque()
        self._active: _QueuedRequest | None = None
        self._closed = False
        self._adapter.add_tr_data_listener(self._handle_tr_data)

    @property
    def pending_count(self) -> int:
        return len(self._pending) + int(self._active is not None)

    def submit(
        self,
        request: TrRequest,
        on_success: Callable[[dict[str, str]], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        if self._closed:
            raise KiwoomTrClosedError("TR request queue is closed")
        self._pending.append(_QueuedRequest(request, on_success, on_error))
        self._start_next()

    def request(self, request: TrRequest) -> dict[str, str]:
        """Wait in a bounded nested Qt loop while the outer UI keeps processing."""
        event_loop = self._event_loop_factory()
        result: list[dict[str, str]] = []
        errors: list[Exception] = []

        def succeed(data: dict[str, str]) -> None:
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
            timer = self._timer_factory()
            queued.timer = timer
            timer.setSingleShot(True)
            timer.timeout.connect(lambda queued=queued: self._handle_timeout(queued))
            immediate_result = self._adapter.request_tr(
                queued.request.request_name,
                queued.request.tr_code,
                0,
                queued.screen_no,
            )
            if immediate_result != 0:
                raise KiwoomTrError(
                    f"CommRqData rejected request with result {immediate_result}"
                )
            if self._active is queued:
                timer.start(queued.request.timeout_ms)
        except Exception as exc:
            self._finish_error(exc)

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

    def _finish_success(self, data: dict[str, str]) -> None:
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
        if queued.screen_no is not None:
            self._screen_pool.release(queued.screen_no)
