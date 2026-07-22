"""Connection-gated briefing dispatch tests."""

from datetime import date

from qz_briefing.briefing import BriefingType
from qz_briefing.kiwoom import ConnectionState
from qz_briefing.scheduling import ConnectionAwareBriefingDispatcher


TRADING_DATE = date(2026, 7, 21)


def make_dispatcher(
    initial_state: ConnectionState = ConnectionState.DISCONNECTED,
):
    state = [initial_state]
    shutting_down = [False]
    dispatcher = ConnectionAwareBriefingDispatcher(
        connection_state=lambda: state[0],
        shutdown_started=lambda: shutting_down[0],
    )
    return dispatcher, state, shutting_down


def test_disconnected_briefing_is_pending() -> None:
    dispatcher, _, _ = make_dispatcher()
    calls: list[str] = []
    assert not dispatcher.dispatch(
        TRADING_DATE, BriefingType.PRE_MARKET, lambda: calls.append("pre")
    )
    assert dispatcher.pending_count == 1
    assert calls == []


def test_connected_event_runs_pending_once() -> None:
    dispatcher, state, _ = make_dispatcher()
    calls: list[str] = []
    dispatcher.dispatch(
        TRADING_DATE, BriefingType.PRE_MARKET, lambda: calls.append("pre")
    )
    state[0] = ConnectionState.CONNECTED
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    assert calls == ["pre"]
    assert dispatcher.pending_count == 0


def test_pre_market_and_intraday_can_both_wait_for_connection() -> None:
    dispatcher, _, _ = make_dispatcher(ConnectionState.CONNECTING)
    calls: list[str] = []
    dispatcher.dispatch(
        TRADING_DATE, BriefingType.PRE_MARKET, lambda: calls.append("pre")
    )
    dispatcher.dispatch(
        TRADING_DATE, BriefingType.INTRADAY_10AM, lambda: calls.append("intra")
    )
    assert dispatcher.pending_count == 2
    dispatcher.on_connection_state(ConnectionState.FAILED)
    assert dispatcher.pending_count == 2
    assert calls == []


def test_connected_state_dispatches_immediately() -> None:
    dispatcher, _, _ = make_dispatcher(ConnectionState.CONNECTED)
    calls: list[str] = []
    assert dispatcher.dispatch(
        TRADING_DATE, BriefingType.PRE_MARKET, lambda: calls.append("pre")
    )
    assert calls == ["pre"]
    assert dispatcher.pending_count == 0


def test_duplicate_pending_key_is_dispatched_only_once() -> None:
    dispatcher, _, _ = make_dispatcher()
    calls: list[str] = []
    dispatcher.dispatch(
        TRADING_DATE, BriefingType.PRE_MARKET, lambda: calls.append("first")
    )
    dispatcher.dispatch(
        TRADING_DATE, BriefingType.PRE_MARKET, lambda: calls.append("duplicate")
    )
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    assert calls == ["first"]


def test_shutdown_clears_pending_and_blocks_new_work() -> None:
    dispatcher, state, shutting_down = make_dispatcher()
    calls: list[str] = []
    dispatcher.dispatch(
        TRADING_DATE, BriefingType.PRE_MARKET, lambda: calls.append("pre")
    )
    shutting_down[0] = True
    dispatcher.stop()
    state[0] = ConnectionState.CONNECTED
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    assert not dispatcher.dispatch(
        TRADING_DATE, BriefingType.INTRADAY_10AM, lambda: calls.append("intra")
    )
    assert dispatcher.pending_count == 0
    assert calls == []


def test_market_close_timer_and_connection_callbacks_dispatch_once() -> None:
    dispatcher, state, shutting_down = make_dispatcher()
    calls: list[str] = []
    callback = lambda: calls.append("close")
    dispatcher.dispatch(TRADING_DATE, BriefingType.MARKET_CLOSE, callback)
    dispatcher.dispatch(TRADING_DATE, BriefingType.MARKET_CLOSE, callback)
    state[0] = ConnectionState.CONNECTED
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    assert calls == ["close"]
    shutting_down[0] = True
    assert not dispatcher.dispatch(TRADING_DATE, BriefingType.MARKET_CLOSE, callback)


def test_failed_regular_briefing_retries_once_after_connection_recovery() -> None:
    dispatcher, state, _ = make_dispatcher(ConnectionState.CONNECTED)
    calls = []
    def callback():
        calls.append("run")
        if len(calls) == 1: raise TimeoutError("temporary disconnect")
    assert dispatcher.dispatch(TRADING_DATE, BriefingType.PRE_MARKET, callback)
    assert dispatcher.pending_count == 1
    state[0] = ConnectionState.CONNECTED
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    assert calls == ["run", "run"] and dispatcher.pending_count == 0


def test_non_transient_briefing_error_is_not_retried() -> None:
    dispatcher, _, _ = make_dispatcher(ConnectionState.CONNECTED)
    calls = []
    dispatcher.dispatch(TRADING_DATE, BriefingType.PRE_MARKET, lambda: (calls.append(1), (_ for _ in ()).throw(ValueError("bad input")))[-1])
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    assert calls == [1] and dispatcher.pending_count == 0


def test_manual_validation_failure_is_not_automatically_retried() -> None:
    dispatcher, _, _ = make_dispatcher(ConnectionState.CONNECTED)
    calls = []
    dispatcher.dispatch(TRADING_DATE, BriefingType.MARKET_CLOSE, lambda: (calls.append(1), (_ for _ in ()).throw(RuntimeError()))[-1], recoverable=False)
    dispatcher.on_connection_state(ConnectionState.CONNECTED)
    assert calls == [1] and dispatcher.pending_count == 0
