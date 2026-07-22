"""Executable QApplication entry point for QZ Briefing."""

from __future__ import annotations

import os
import sys
import argparse
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from qz_briefing.briefing import (
    BriefingStorage,
    BriefingType,
    DailyBriefingPipeline,
    HoldingsCollector,
    KiwoomDerivativesDataSource,
    KiwoomDerivativesFlowCollector,
    KiwoomCoreMarketCollector,
    KiwoomMarketIndexCollector,
    KiwoomMarketIndexDataSource,
    KiwoomInvestorFlowCollector,
    KiwoomInvestorFlowDataSource,
    KiwoomLeadershipCollector,
    KiwoomLeadershipDataSource,
    KiwoomAccountHoldingsSource,
    KiwoomStockBasicDataSource,
    UnavailableFuturesContractResolver,
)
from qz_briefing.kiwoom import (
    ConnectionState,
    ConnectionTransition,
    KiwoomConnectionManager,
    KiwoomQAxAdapter,
    KiwoomTrRequestQueue,
)
from qz_briefing.runtime import QtConnectionRuntime
from qz_briefing.runtime.automatic_shutdown import GracefulShutdownController
from qz_briefing.scheduling import (
    BriefingScheduler,
    ConnectionAwareBriefingDispatcher,
    MarketStatus,
    TradingDayResult,
    load_market_calendar,
)


class SignalLike(Protocol):
    def connect(self, callback: Callable[[], None]) -> None: ...


class ApplicationLike(Protocol):
    aboutToQuit: SignalLike

    def exec_(self) -> int: ...

    def quit(self) -> None: ...


class ProcessLockLike(Protocol):
    def tryLock(self, timeout: int = 0) -> bool: ...

    def removeStaleLockFile(self) -> bool: ...

    def unlock(self) -> None: ...


ApplicationFactory = Callable[[Sequence[str]], ApplicationLike]
AdapterFactory = Callable[[], KiwoomQAxAdapter]
ManagerFactory = Callable[[KiwoomQAxAdapter], KiwoomConnectionManager]
RuntimeFactory = Callable[..., QtConnectionRuntime]
LockFactory = Callable[[], ProcessLockLike]
ShutdownControllerFactory = Callable[
    [ApplicationLike, ProcessLockLike], GracefulShutdownController
]
MarketDayChecker = Callable[[date], TradingDayResult]
BriefingSchedulerFactory = Callable[
    [dict[str, Callable[[], None]]], BriefingScheduler
]
LocalClock = Callable[[], datetime]
BriefingPipelineFactory = Callable[
    [LocalClock, KiwoomTrRequestQueue], DailyBriefingPipeline
]
TrQueueFactory = Callable[[KiwoomQAxAdapter], KiwoomTrRequestQueue]
DashboardFactory = Callable[..., object]


def create_dashboard(**kwargs: object) -> object:
    from qz_briefing.ui import DashboardMainWindow
    return DashboardMainWindow(**kwargs)


def parse_cli_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options before creating QApplication or the Kiwoom OCX."""
    raw = list(sys.argv[1:] if arguments is None else arguments)
    if raw and not raw[0].startswith("-"):
        raw = raw[1:]
    parser = argparse.ArgumentParser(prog="python -m qz_briefing")
    parser.add_argument(
        "--run-now",
        choices=(BriefingType.MARKET_CLOSE.value,),
        dest="run_now",
        help="run one briefing immediately after Kiwoom login",
    )
    return parser.parse_args(raw)


def check_market_day(target_date: date) -> TradingDayResult:
    """Evaluate the maintained offline KRX calendar."""
    return load_market_calendar().evaluate(target_date)


def create_briefing_pipeline(
    clock: LocalClock, tr_queue: KiwoomTrRequestQueue
) -> DailyBriefingPipeline:
    """Build one process-wide offline pipeline and its result storage."""
    storage = BriefingStorage(
        Path(__file__).resolve().parents[2] / "data" / "briefings"
    )
    project_root = Path(__file__).resolve().parents[2]
    selected_leadership_codes: set[str] = set()
    stock_source = KiwoomStockBasicDataSource(tr_queue)
    daily_source = KiwoomLeadershipDataSource(tr_queue)

    def update_leadership_codes(codes: set[str]) -> None:
        selected_leadership_codes.clear()
        selected_leadership_codes.update(codes)
    return DailyBriefingPipeline(
        storage,
        [
            KiwoomCoreMarketCollector(
                stock_source, clock=clock
            ),
            KiwoomMarketIndexCollector(
                KiwoomMarketIndexDataSource(tr_queue), clock=clock
            ),
            KiwoomInvestorFlowCollector(
                KiwoomInvestorFlowDataSource(tr_queue), clock=clock
            ),
            KiwoomDerivativesFlowCollector(
                UnavailableFuturesContractResolver(),
                KiwoomDerivativesDataSource(tr_queue),
                clock=clock,
            ),
            KiwoomLeadershipCollector(
                daily_source,
                clock=clock,
                on_selected=update_leadership_codes,
            ),
            HoldingsCollector(
                project_root / "config" / "holdings.json",
                stock_source,
                daily_source,
                leadership_codes=lambda: set(selected_leadership_codes),
                account_source=KiwoomAccountHoldingsSource(
                    tr_queue.adapter, tr_queue
                ),
                clock=clock,
            ),
        ],
        clock=clock,
    )


def create_single_instance_lock() -> ProcessLockLike:
    """Create a per-user lock in Local AppData without creating QApplication."""
    from PyQt5.QtCore import QLockFile

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not available")
    lock_directory = Path(local_app_data) / "QZ_Briefing"
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_directory / "qz_briefing.lock"))
    lock.setStaleLockTime(30_000)
    return lock


def acquire_process_lock(lock: ProcessLockLike) -> bool:
    """Acquire once, recovering only a lock QLockFile confirms is stale."""
    if lock.tryLock(0):
        return True
    if not lock.removeStaleLockFile():
        return False
    return bool(lock.tryLock(0))


def create_application(arguments: Sequence[str]) -> ApplicationLike:
    """Return the process QApplication, creating it exactly once if necessary."""
    from PyQt5.QtWidgets import QApplication

    application = QApplication.instance()
    if application is None:
        application = QApplication(list(arguments))
    application.setQuitOnLastWindowClosed(False)
    return application


class ConsoleConnectionReporter:
    """Print new, non-sensitive connection transitions to the console."""

    def __init__(
        self,
        manager: KiwoomConnectionManager,
        adapter: KiwoomQAxAdapter | None = None,
        on_connection_state: Callable[[ConnectionState], None] | None = None,
    ) -> None:
        self._manager = manager
        self._adapter = adapter
        self._reported_transition_count = 0
        self._reported_connect_request_count = -1
        self._reported_login_event_count = 0
        self._on_connection_state = on_connection_state

    def __call__(self, runtime: QtConnectionRuntime) -> None:
        transitions = self._manager.transitions
        for transition in transitions[self._reported_transition_count :]:
            self._report_transition(transition)
        self._reported_transition_count = len(transitions)
        self._report_adapter_diagnostics()
        if self._on_connection_state is not None:
            self._on_connection_state(runtime.connection_state)

    def _report_adapter_diagnostics(self) -> None:
        if self._adapter is None:
            return
        if self._adapter.connect_request_count != self._reported_connect_request_count:
            self._reported_connect_request_count = self._adapter.connect_request_count
            print(
                f"COMMCONNECT CALL COUNT: {self._adapter.connect_request_count}",
                flush=True,
            )
        if self._adapter.login_event_count <= self._reported_login_event_count:
            return
        self._reported_login_event_count = self._adapter.login_event_count
        print(
            f"ONEVENTCONNECT ERROR CODE: {self._adapter.last_login_error_code}",
            flush=True,
        )
        print(
            f"GETCONNECTSTATE RESULT: {self._adapter.last_connect_state}",
            flush=True,
        )

    @staticmethod
    def _report_transition(transition: ConnectionTransition) -> None:
        print(
            "CONNECTION_STATE "
            f"{transition.previous_state.name} -> {transition.new_state.name}: "
            f"{transition.reason}",
            flush=True,
        )
        if transition.reason == "login event confirmed connected state":
            print("LOGIN SUCCESS", flush=True)
        elif transition.reason.startswith("login event reported an error"):
            print("LOGIN FAILED", flush=True)


def run(
    arguments: Sequence[str] | None = None,
    *,
    application_factory: ApplicationFactory = create_application,
    adapter_factory: AdapterFactory = KiwoomQAxAdapter,
    manager_factory: ManagerFactory = KiwoomConnectionManager,
    runtime_factory: RuntimeFactory = QtConnectionRuntime,
    lock_factory: LockFactory = create_single_instance_lock,
    shutdown_controller_factory: ShutdownControllerFactory = GracefulShutdownController,
    market_day_checker: MarketDayChecker = check_market_day,
    briefing_scheduler_factory: BriefingSchedulerFactory = BriefingScheduler,
    briefing_pipeline_factory: BriefingPipelineFactory = create_briefing_pipeline,
    tr_queue_factory: TrQueueFactory = KiwoomTrRequestQueue,
    dashboard_factory: DashboardFactory | None = create_dashboard,
    clock: LocalClock = datetime.now,
) -> int:
    """Assemble the connection runtime and keep the Qt event loop running."""
    options = parse_cli_arguments(arguments)
    manual_market_close = options.run_now == BriefingType.MARKET_CLOSE.value
    if manual_market_close:
        print("manual briefing requested: market_close", flush=True)
    process_lock = lock_factory()
    if not acquire_process_lock(process_lock):
        print("QZ BRIEFING ALREADY RUNNING", flush=True)
        return 2

    shutdown_controller: GracefulShutdownController | None = None
    adapter: KiwoomQAxAdapter | None = None
    runtime: QtConnectionRuntime | None = None
    briefing_scheduler: BriefingScheduler | None = None
    try:
        application = application_factory(sys.argv if arguments is None else arguments)
        print(f"PROCESS PID: {os.getpid()}", flush=True)
        print("QAPPLICATION READY", flush=True)
        shutdown_controller = shutdown_controller_factory(application, process_lock)
        application.aboutToQuit.connect(shutdown_controller.handle_application_quit)
        if not shutdown_controller.schedule():
            return 0

        now = clock()
        trading_day = market_day_checker(now.date())
        print(
            "TRADING DAY "
            f"date={trading_day.date.isoformat()} "
            f"status={trading_day.status.value} "
            f"reason={trading_day.reason}",
            flush=True,
        )
        if trading_day.warning is not None:
            print(f"TRADING CALENDAR WARNING: {trading_day.warning}", flush=True)
        if trading_day.status is MarketStatus.UNKNOWN:
            print(
                "market calendar incomplete; continuing in warning mode",
                flush=True,
            )
        if trading_day.status is MarketStatus.CLOSED:
            shutdown_controller.request_shutdown(
                "non-trading day shutdown requested: "
                f"date={trading_day.date.isoformat()} reason={trading_day.reason}"
            )
            return 0

        adapter = adapter_factory()
        print("KIWOOM OCX READY", flush=True)
        manager = manager_factory(adapter)
        tr_queue = tr_queue_factory(adapter)
        if hasattr(tr_queue, "set_timeout_observer"):
            def observe_tr_timeout(count: int) -> None:
                if count < 2: return
                if hasattr(tr_queue, "pause"): tr_queue.pause("consecutive TR timeouts detected")
                manager.request_connection_recheck("consecutive TR timeouts detected")
            tr_queue.set_timeout_observer(observe_tr_timeout)
        shutdown_controller.attach_briefing_scheduler(tr_queue)
        pipeline = briefing_pipeline_factory(clock, tr_queue)
        dispatcher = ConnectionAwareBriefingDispatcher(
            connection_state=lambda: manager.state,
            shutdown_started=lambda: shutdown_controller.shutdown_started,
        )
        dashboard = None
        try:
            if dashboard_factory is None:
                raise LookupError("dashboard disabled by caller")
            dashboard = dashboard_factory(
                root=pipeline.storage_root,
                connection_state=lambda: manager.state,
                trading_day_status=trading_day.status.value,
                shutdown=lambda: shutdown_controller.request_shutdown(
                    "dashboard tray shutdown requested"
                ),
                clock=clock,
            )
            if dashboard is not None:
                pipeline.add_completion_listener(
                    lambda name, path: dashboard.handle_briefing_completed(name)
                )
                shutdown_controller.attach_briefing_scheduler(dashboard)
                dashboard.show()
        except LookupError:
            pass
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "dashboard initialization failed; briefing runtime will continue"
            )

        def run_briefing(briefing_type: BriefingType) -> None:
            dispatcher.dispatch(
                now.date(),
                briefing_type,
                lambda: pipeline.run(
                    briefing_type,
                    now.date(),
                    market_calendar_status=trading_day.status.value,
                    market_calendar_reason=trading_day.reason,
                    market_calendar_warning=trading_day.warning,
                ),
            )

        callbacks = {
                BriefingType.PRE_MARKET.value: lambda: run_briefing(
                    BriefingType.PRE_MARKET
                ),
                BriefingType.INTRADAY_10AM.value: lambda: run_briefing(
                    BriefingType.INTRADAY_10AM
                ),
                BriefingType.MARKET_CLOSE.value: lambda: run_briefing(
                    BriefingType.MARKET_CLOSE
                ),
            }
        if not manual_market_close:
            briefing_scheduler = briefing_scheduler_factory(callbacks)
        shutdown_controller.attach_briefing_scheduler(dispatcher)
        if briefing_scheduler is not None:
            shutdown_controller.attach_briefing_scheduler(briefing_scheduler)
        def report_connection_state(state: ConnectionState) -> None:
            if state is ConnectionState.CONNECTED and hasattr(tr_queue, "resume"):
                tr_queue.resume()
            elif state in {ConnectionState.RECHECKING, ConnectionState.RECONNECT_WAIT, ConnectionState.RECONNECTING, ConnectionState.FAILED} and hasattr(tr_queue, "pause"):
                tr_queue.pause(f"connection recovery state: {state.name}")
            dispatcher.on_connection_state(state)
            if dashboard is not None and hasattr(dashboard, "handle_connection_state"):
                dashboard.handle_connection_state(state)

        reporter = ConsoleConnectionReporter(
            manager,
            adapter,
            on_connection_state=report_connection_state,
        )
        runtime = runtime_factory(adapter, manager, on_state_change=reporter)
        shutdown_controller.attach_runtime(runtime)
        if not runtime.start():
            raise RuntimeError("Kiwoom connection runtime did not start")
        print("RUNTIME STARTED", flush=True)
        if manual_market_close:
            def execute_validation() -> None:
                try:
                    pipeline.run(
                        BriefingType.MARKET_CLOSE,
                        now.date(),
                        market_calendar_status=trading_day.status.value,
                        market_calendar_reason=trading_day.reason,
                        market_calendar_warning=trading_day.warning,
                        manual_validation=True,
                    )
                finally:
                    print("manual validation completed; shutting down", flush=True)
                    shutdown_controller.request_shutdown(
                        "manual validation shutdown requested"
                    )

            dispatcher.dispatch(
                now.date(), BriefingType.MARKET_CLOSE, execute_validation,
                recoverable=False,
            )
        else:
            briefing_scheduler.schedule(now)
        return int(application.exec_())
    finally:
        if runtime is None and adapter is not None:
            adapter.close()
        if shutdown_controller is None:
            process_lock.unlock()
        else:
            shutdown_controller.handle_application_quit()
        if adapter is not None:
            print("RUNTIME STOPPED", flush=True)


def main() -> int:
    """Run QZ Briefing and report startup failures without exposing credentials."""
    try:
        return run()
    except KeyboardInterrupt:
        print("shutdown requested by user", flush=True)
        return 0
    except Exception as exc:
        print(
            f"STARTUP FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
