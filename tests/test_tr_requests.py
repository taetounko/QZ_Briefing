"""Sequential Kiwoom TR queue tests with no real OCX calls."""

import pytest

from qz_briefing.kiwoom import (
    KiwoomTrClosedError,
    KiwoomTrRequestQueue,
    KiwoomTrTimeoutError,
    TrRequest,
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
        self.started_with: int | None = None
        self.stop_count = 0

    def setSingleShot(self, single_shot: bool) -> None:
        self.single_shot = single_shot

    def start(self, milliseconds: int) -> None:
        self.started_with = milliseconds

    def stop(self) -> None:
        self.stop_count += 1


class FakeTrAdapter:
    def __init__(self) -> None:
        self.listener: object | None = None
        self.inputs: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str, int, str]] = []
        self.values: dict[tuple[str, str, int, str], str] = {}
        self.repeat_count = 0
        self.request_results: list[int] = []

    def add_tr_data_listener(self, callback: object) -> None:
        self.listener = callback

    def set_input_value(self, item: str, value: str) -> None:
        self.inputs.append((item, value))

    def request_tr(
        self, request_name: str, tr_code: str, previous_next: int, screen_no: str
    ) -> int:
        self.requests.append((request_name, tr_code, previous_next, screen_no))
        return self.request_results.pop(0) if self.request_results else 0

    def get_comm_data(
        self, tr_code: str, request_name: str, index: int, item_name: str
    ) -> str:
        return self.values[(tr_code, request_name, index, item_name)]

    def get_repeat_count(self, tr_code: str, request_name: str) -> int:
        return self.repeat_count

    def respond(self, request_index: int = -1, previous_next: str = "0") -> None:
        request_name, tr_code, _, screen_no = self.requests[request_index]
        self.listener(screen_no, request_name, tr_code, "주식기본정보", previous_next)  # type: ignore[operator]


def request(name: str = "stock") -> TrRequest:
    return TrRequest(
        request_name=name,
        tr_code="OPT10001",
        inputs={"종목코드": "005930"},
        output_fields=("현재가", "등락율"),
        timeout_ms=3210,
    )


def make_queue(*, minimum_interval_ms=0, overload_backoff_ms=(3000, 7000, 15000), monotonic=lambda: 0.0):
    adapter = FakeTrAdapter()
    timers: list[FakeTimer] = []

    def timer_factory() -> FakeTimer:
        timer = FakeTimer()
        timers.append(timer)
        return timer

    queue = KiwoomTrRequestQueue(
        adapter,
        timer_factory=timer_factory,
        minimum_interval_ms=minimum_interval_ms,
        overload_backoff_ms=overload_backoff_ms,
        monotonic=monotonic,
    )
    return queue, adapter, timers


def test_request_sets_inputs_and_calls_comm_rq_data() -> None:
    queue, adapter, timers = make_queue()
    queue.submit(request(), lambda data: None, lambda error: None)
    assert adapter.inputs == [("종목코드", "005930")]
    assert adapter.requests == [("stock", "OPT10001", 0, "1000")]
    assert timers[0].single_shot
    assert timers[0].started_with == 3210


def test_matching_response_parses_requested_fields() -> None:
    queue, adapter, _ = make_queue()
    adapter.values = {
        ("OPT10001", "stock", 0, "현재가"): "+72,500",
        ("OPT10001", "stock", 0, "등락율"): "+1.25",
    }
    results: list[dict[str, str]] = []
    queue.submit(request(), results.append, lambda error: None)
    adapter.respond()
    assert results == [{"현재가": "+72,500", "등락율": "+1.25"}]


def test_timeout_completes_request_with_error() -> None:
    queue, _, timers = make_queue()
    errors: list[Exception] = []
    queue.submit(request(), lambda data: None, errors.append)
    timers[0].timeout.emit()
    assert len(errors) == 1
    assert isinstance(errors[0], KiwoomTrTimeoutError)
    assert queue.pending_count == 0


def test_repeated_response_does_not_complete_twice() -> None:
    queue, adapter, _ = make_queue()
    adapter.values = {
        ("OPT10001", "stock", 0, "현재가"): "72500",
        ("OPT10001", "stock", 0, "등락율"): "1.25",
    }
    results: list[dict[str, str]] = []
    queue.submit(request(), results.append, lambda error: None)
    adapter.respond()
    adapter.respond()
    assert len(results) == 1


def test_requests_are_sent_sequentially_without_screen_collision() -> None:
    queue, adapter, _ = make_queue()
    for name in ("first", "second"):
        adapter.values[("OPT10001", name, 0, "현재가")] = "72500"
        adapter.values[("OPT10001", name, 0, "등락율")] = "1.25"
    results: list[str] = []
    queue.submit(request("first"), lambda data: results.append("first"), lambda e: None)
    queue.submit(request("second"), lambda data: results.append("second"), lambda e: None)
    assert [item[0] for item in adapter.requests] == ["first"]
    adapter.respond(0)
    assert [item[0] for item in adapter.requests] == ["first", "second"]
    assert adapter.requests[0][3] == "1000"
    assert adapter.requests[1][3] == "1001"
    adapter.respond(1)
    assert results == ["first", "second"]


def test_stale_timeout_cannot_fail_the_next_request() -> None:
    queue, adapter, timers = make_queue()
    for name in ("first", "second"):
        adapter.values[("OPT10001", name, 0, "현재가")] = "72500"
        adapter.values[("OPT10001", name, 0, "등락율")] = "1.25"
    errors: list[Exception] = []
    results: list[str] = []
    queue.submit(request("first"), lambda data: results.append("first"), errors.append)
    queue.submit(request("second"), lambda data: results.append("second"), errors.append)
    adapter.respond(0)
    timers[0].timeout.emit()
    adapter.respond(1)
    assert errors == []
    assert results == ["first", "second"]


def test_close_cancels_active_and_pending_requests() -> None:
    queue, _, _ = make_queue()
    errors: list[Exception] = []
    queue.submit(request("first"), lambda data: None, errors.append)
    queue.submit(request("second"), lambda data: None, errors.append)
    queue.close()
    assert len(errors) == 2
    assert all(isinstance(error, KiwoomTrClosedError) for error in errors)
    assert queue.pending_count == 0


def test_closed_queue_rejects_new_requests() -> None:
    queue, _, _ = make_queue()
    queue.close()
    with pytest.raises(KiwoomTrClosedError):
        queue.submit(request(), lambda data: None, lambda error: None)


def test_repeated_response_parses_every_official_output_row() -> None:
    queue, adapter, _ = make_queue()
    repeated = TrRequest(
        request_name="flows",
        tr_code="OPT10051",
        inputs={"시장구분": "0"},
        output_fields=("업종코드", "개인순매수"),
        repeat=True,
    )
    adapter.repeat_count = 2
    adapter.values = {
        ("OPT10051", "flows", 0, "업종코드"): "001",
        ("OPT10051", "flows", 0, "개인순매수"): "-1,000",
        ("OPT10051", "flows", 1, "업종코드"): "002",
        ("OPT10051", "flows", 1, "개인순매수"): "2,000",
    }
    results: list[object] = []
    queue.submit(repeated, results.append, lambda error: None)
    adapter.respond()
    assert results == [
        [
            {"업종코드": "001", "개인순매수": "-1,000"},
            {"업종코드": "002", "개인순매수": "2,000"},
        ]
    ]


def test_paginated_rows_request_continuation_and_finish_once() -> None:
    queue, adapter, _ = make_queue()
    request_value = TrRequest("account", "OPW00018", {}, ("종목번호",), repeat=True, paginate=True)
    adapter.repeat_count = 1
    adapter.values[("OPW00018", "account", 0, "종목번호")] = "A005930"
    results = []
    queue.submit(request_value, results.append, lambda error: None)
    adapter.respond(previous_next="2")
    assert adapter.requests[-1][2] == 2 and results == []
    adapter.values[("OPW00018", "account", 0, "종목번호")] = "A000660"
    adapter.respond(request_index=1, previous_next="0")
    assert results == [[{"종목번호": "A005930"}, {"종목번호": "A000660"}]]


def test_first_request_is_immediate_and_next_waits_for_global_interval() -> None:
    now = [0.0]
    queue, adapter, timers = make_queue(minimum_interval_ms=1000, monotonic=lambda: now[0])
    queue.submit(request("first"), lambda data: None, lambda error: None)
    assert [item[0] for item in adapter.requests] == ["first"]
    adapter.values.update({("OPT10001", "first", 0, "현재가"): "1", ("OPT10001", "first", 0, "등락율"): "0"})
    adapter.respond()
    queue.submit(request("second"), lambda data: None, lambda error: None)
    assert [item[0] for item in adapter.requests] == ["first"]
    assert timers[-1].started_with == 1000
    now[0] = 1.0; timers[-1].timeout.emit()
    assert [item[0] for item in adapter.requests] == ["first", "second"]


def test_overload_retries_with_three_backoffs_then_succeeds() -> None:
    queue, adapter, timers = make_queue()
    adapter.request_results = [-200, -200, -200, 0]
    results = []
    queue.submit(request(), results.append, lambda error: None)
    for expected in (3000, 7000, 15000):
        assert timers[-1].started_with == expected
        timers[-1].timeout.emit()
    assert len(adapter.requests) == 4
    adapter.values = {("OPT10001", "stock", 0, "현재가"): "1", ("OPT10001", "stock", 0, "등락율"): "0"}
    adapter.respond()
    assert len(results) == 1


def test_overload_exhaustion_and_non_overload_no_retry() -> None:
    queue, adapter, timers = make_queue()
    adapter.request_results = [-200, -200, -200, -200]
    errors = []
    queue.submit(request(), lambda data: None, errors.append)
    for expected in (3000, 7000, 15000):
        assert timers[-1].started_with == expected
        timers[-1].timeout.emit()
    assert len(errors) == 1 and len(adapter.requests) == 4

    queue2, adapter2, timers2 = make_queue()
    adapter2.request_results = [-201]
    errors2 = []
    queue2.submit(request(), lambda data: None, errors2.append)
    assert len(errors2) == 1 and len(adapter2.requests) == 1 and timers2 == []


def test_retry_preserves_order_and_shutdown_cancels_retry() -> None:
    queue, adapter, timers = make_queue()
    adapter.request_results = [-200]
    errors = []
    queue.submit(request("first"), lambda data: None, errors.append)
    queue.submit(request("second"), lambda data: None, errors.append)
    retry_timer = timers[-1]
    assert [item[0] for item in adapter.requests] == ["first"]
    queue.close(); retry_timer.timeout.emit()
    assert retry_timer.stop_count >= 1
    assert [item[0] for item in adapter.requests] == ["first"] and len(errors) == 2


def test_shutdown_cancels_global_interval_timer() -> None:
    queue, adapter, timers = make_queue(minimum_interval_ms=1000)
    adapter.values = {("OPT10001", "first", 0, "현재가"): "1", ("OPT10001", "first", 0, "등락율"): "0"}
    queue.submit(request("first"), lambda data: None, lambda error: None); adapter.respond()
    errors = []
    queue.submit(request("second"), lambda data: None, errors.append)
    interval_timer = timers[-1]
    queue.close(); interval_timer.timeout.emit()
    assert interval_timer.stop_count >= 1
    assert [item[0] for item in adapter.requests] == ["first"] and len(errors) == 1


def test_progress_diagnostics_and_two_timeouts_request_connection_recheck() -> None:
    observed = []
    queue, adapter, timers = make_queue()
    queue.set_timeout_observer(observed.append)
    queue.submit(request("first"), lambda data: None, lambda error: None)
    assert queue.progress["active_request"] == "first"
    assert queue.progress["last_request_started_at"] == 0.0
    timers[-1].timeout.emit()
    queue.submit(request("second"), lambda data: None, lambda error: None)
    timers[-1].timeout.emit()
    assert observed == [1, 2]
    assert queue.progress["active_request"] is None
    assert queue.progress["consecutive_timeouts"] == 2


def test_success_resets_consecutive_timeout_count_and_late_response_is_ignored() -> None:
    queue, adapter, timers = make_queue()
    results = []
    queue.submit(request("first"), results.append, lambda error: None)
    first_request = adapter.requests[0]
    timers[-1].timeout.emit()
    queue.submit(request("second"), results.append, lambda error: None)
    # A late response with the old request name/screen cannot complete the active request.
    adapter.listener(first_request[3], first_request[0], first_request[1], "", "0")
    assert results == [] and queue.progress["active_request"] == "second"
    adapter.values = {("OPT10001", "second", 0, "현재가"): "1", ("OPT10001", "second", 0, "등락율"): "0"}
    adapter.respond()
    assert queue.progress["consecutive_timeouts"] == 0
    assert queue.progress["last_response_at"] == 0.0


def test_pause_fails_active_preserves_pending_and_resume_continues() -> None:
    queue, adapter, _ = make_queue()
    errors = []
    queue.submit(request("active"), lambda data: None, errors.append)
    queue.submit(request("pending"), lambda data: None, errors.append)
    queue.pause("connection lost")
    assert len(errors) == 1 and queue.progress["active_request"] is None
    assert [row[0] for row in adapter.requests] == ["active"]
    queue.resume()
    assert [row[0] for row in adapter.requests] == ["active", "pending"]


def test_shutdown_while_paused_cancels_preserved_pending() -> None:
    queue, _, _ = make_queue(); errors = []
    queue.submit(request("active"), lambda data: None, errors.append)
    queue.submit(request("pending"), lambda data: None, errors.append)
    queue.pause(); queue.close()
    assert len(errors) == 2 and queue.pending_count == 0
