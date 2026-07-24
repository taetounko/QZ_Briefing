"""Executable QApplication entry point for QZ Briefing."""

from __future__ import annotations

import os
import sys
import argparse
import getpass
import json
from collections.abc import Callable, Sequence
from datetime import date, datetime, time
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
    KiwoomPreopenRealSource,
    PreopenMonitoringController,
    UnavailableFuturesContractResolver,
)
from qz_briefing.kiwoom import (
    ConnectionState,
    ConnectionTransition,
    KiwoomConnectionManager,
    KiwoomQAxAdapter,
    KiwoomTrRequestQueue,
)
from qz_briefing.runtime import (
    MissingBriefingRecovery, QtConnectionRuntime, RuntimeMonitor,
    SleepInhibitor, configure_daily_logging,
)
from qz_briefing.runtime.automatic_shutdown import GracefulShutdownController
from qz_briefing.notifications import (
    DisabledNotificationService, DpapiSecretStore, NotificationRequest,
    NotificationService, PersistentNotificationQueue, TelegramAdapter,
    format_briefing,
)
from qz_briefing.notifications.formatter import format_daily_summary, format_runtime_alert
from qz_briefing.runtime.unattended import atomic_write_json
from qz_briefing.scheduling import (
    BriefingScheduler,
    PREOPEN_MONITORING,
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
SleepInhibitorFactory = Callable[[], SleepInhibitor]
RuntimeMonitorFactory = Callable[..., RuntimeMonitor]
NotificationServiceFactory = Callable[[Path, Path, object], object]


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
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--configure-telegram", action="store_true")
    commands.add_argument("--disable-telegram", action="store_true")
    commands.add_argument("--test-notification", action="store_true")
    commands.add_argument("--notification-status", action="store_true")
    commands.add_argument("--validate-unattended-cycle", action="store_true")
    commands.add_argument("--validate-stock-recommendations", action="store_true")
    commands.add_argument("--validate-recommendation-data-pipeline", action="store_true")
    commands.add_argument("--plan-live-recommendation-collection", action="store_true")
    commands.add_argument("--collect-recommendation-data", action="store_true")
    commands.add_argument("--diagnose-opt10081-live", action="store_true")
    commands.add_argument("--diagnose-kiwoom-login", action="store_true")
    commands.add_argument("--validate-live-recommendation-collection", action="store_true")
    commands.add_argument("--diagnose-opt10059-live", action="store_true")
    commands.add_argument("--validate-cached-opt10059-candidates", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--mode", choices=("bootstrap","daily_incremental","repair"))
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--price-only", action="store_true")
    parser.add_argument("--remove-secret", action="store_true", help=argparse.SUPPRESS)
    parsed = parser.parse_args(raw)
    if parsed.remove_secret and not parsed.disable_telegram:
        parser.error("--remove-secret requires --disable-telegram")
    if parsed.diagnose_opt10081_live and not parsed.symbol:
        parser.error("--diagnose-opt10081-live requires --symbol")
    return parsed


def mask_chat_id(value: str) -> str:
    return "*" * max(0, len(value) - 4) + value[-4:]


def load_notification_config(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def load_telegram_credentials(store: object, legacy_chat_id: str = "") -> tuple[str, str]:
    """Load the DPAPI payload, accepting the original token-only format."""
    plaintext = store.load()
    try:
        payload = json.loads(plaintext)
    except (TypeError, ValueError):
        return str(plaintext), legacy_chat_id
    if not isinstance(payload, dict):
        return str(plaintext), legacy_chat_id
    return str(payload.get("token", "")), str(payload.get("chat_id", ""))


def record_manual_notification_delivery(project_root: Path, event_type: str) -> None:
    history_path = project_root / "data" / "runtime" / "notification_delivery_history.json"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    if not isinstance(history, list):
        history = []
    delivered_at = datetime.now().isoformat()
    history.append({"key": f"manual_test|{delivered_at}", "delivered_at": delivered_at, "event_type": event_type})
    atomic_write_json(history_path, history)


def create_notification_service(project_root: Path, data_root: Path, timer_factory=None):
    config = load_notification_config(project_root / "config" / "notifications.json")
    telegram = config.get("telegram") if isinstance(config.get("telegram"), dict) else {}
    if not telegram.get("enabled"):
        print("Telegram notifications disabled", flush=True)
        return DisabledNotificationService()
    try:
        token, chat_id = load_telegram_credentials(
            DpapiSecretStore(project_root / "config" / "telegram_token.dpapi"),
            str(telegram.get("chat_id", "")),
        )
        adapter = TelegramAdapter(token, chat_id)
        return NotificationService(
            adapter, PersistentNotificationQueue(data_root / "runtime" / "notification_queue.json"),
            data_root / "runtime" / "notification_delivery_history.json",
            send_markdown_file=bool(telegram.get("send_markdown_file", True)),
            send_json_file=bool(telegram.get("send_json_file", False)),
            send_runtime_alerts=bool(telegram.get("send_runtime_alerts", True)),
            send_daily_summary=bool(telegram.get("send_daily_summary", True)),
            timer_factory=timer_factory,
        )
    except Exception:
        print("Telegram token unavailable; notifications disabled", flush=True)
        return DisabledNotificationService()


def enqueue_saved_briefing(notification_service: object, name: str, path: str) -> bool:
    """Queue a normal briefing only after its persisted JSON can be loaded."""
    if "validation" in name:
        return False
    json_path = Path(path)
    result = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        return False
    event_type = str(result.get("briefing_type", ""))
    if event_type not in {"pre_market", "intraday_10am", "market_close"}:
        return False
    return bool(notification_service.submit(NotificationRequest(
        event_type=event_type,
        trading_date=str(result.get("trading_date", "")),
        text=format_briefing(result),
        markdown_path=str(json_path.with_suffix(".md")),
        json_path=str(json_path),
    )))
def handle_notification_cli(options: argparse.Namespace, project_root: Path, *, input_secret=getpass.getpass, input_text=input, adapter_factory=TelegramAdapter, secret_store_factory=DpapiSecretStore) -> int | None:
    if not (options.configure_telegram or options.disable_telegram or options.test_notification or options.notification_status):
        return None
    config_path = project_root / "config" / "notifications.json"; secret_path = project_root / "config" / "telegram_token.dpapi"
    config = load_notification_config(config_path); telegram = config.get("telegram") if isinstance(config.get("telegram"), dict) else {}
    if options.disable_telegram:
        telegram = {**telegram, "enabled": False}; atomic_write_json(config_path, {"telegram": telegram})
        if options.remove_secret: secret_store_factory(secret_path).remove()
        print("Telegram notifications disabled", flush=True); return 0
    if options.configure_telegram:
        token = input_secret("Telegram Bot Token (hidden): "); chat_id = input_secret("Telegram Chat ID (hidden): ").strip()
        adapter_factory(token, chat_id).send_text("QZ Briefing Telegram 연결 테스트", parse_mode=None)
        secret_store_factory(secret_path).save(json.dumps({"token": token, "chat_id": chat_id}, ensure_ascii=False))
        telegram = {key: value for key, value in telegram.items() if key != "chat_id"}
        telegram = {**telegram, "enabled": True, "send_markdown_file": telegram.get("send_markdown_file", True), "send_json_file": telegram.get("send_json_file", False), "send_runtime_alerts": telegram.get("send_runtime_alerts", True), "send_daily_summary": telegram.get("send_daily_summary", True)}
        atomic_write_json(config_path, {"telegram": telegram})
        record_manual_notification_delivery(project_root, "configuration_test")
        print(f"Telegram configured: chat={mask_chat_id(chat_id)}", flush=True); return 0
    token, chat_id = load_telegram_credentials(secret_store_factory(secret_path), str(telegram.get("chat_id", "")))
    runtime_root = project_root / "data" / "runtime"
    history_path = runtime_root / "notification_delivery_history.json"
    queue_path = runtime_root / "notification_queue.json"
    if options.notification_status:
        try: history = json.loads(history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError): history = []
        try: pending = json.loads(queue_path.read_text(encoding="utf-8"))
        except (OSError, ValueError): pending = []
        last = history[-1] if isinstance(history, list) and history else {}
        failures = [item for item in pending if isinstance(item, dict) and item.get("last_error")] if isinstance(pending, list) else []
        print(f"Telegram enabled: {bool(telegram.get('enabled'))}", flush=True)
        print(f"DPAPI credentials restored: {bool(token and chat_id)}", flush=True)
        print(f"Chat: {mask_chat_id(chat_id)}", flush=True)
        print(f"Last success: {last.get('delivered_at') or 'none'}", flush=True)
        print(f"Pending messages: {len(pending) if isinstance(pending, list) else 0}", flush=True)
        print(f"Last failure: {(failures[-1].get('last_error') if failures else None) or 'none'}", flush=True)
        print(f"Delivery history entries: {len(history) if isinstance(history, list) else 0}", flush=True)
        return 0
    adapter_factory(token, chat_id).send_text("QZ Briefing 테스트 알림", parse_mode=None)
    record_manual_notification_delivery(project_root, "test_notification")
    print(f"Test notification sent: chat={mask_chat_id(chat_id)}", flush=True); return 0


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
            print("auto_login_success", flush=True)
        elif transition.reason.startswith("login event reported an error"):
            print("LOGIN FAILED", flush=True)
            print("auto_login_failed", flush=True)


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
    sleep_inhibitor_factory: SleepInhibitorFactory = SleepInhibitor,
    runtime_monitor_factory: RuntimeMonitorFactory = RuntimeMonitor,
    notification_service_factory: NotificationServiceFactory = create_notification_service,
    logging_configurator: Callable[[Path], object] = configure_daily_logging,
    clock: LocalClock = datetime.now,
) -> int:
    """Assemble the connection runtime and keep the Qt event loop running."""
    options = parse_cli_arguments(arguments)
    project_root = Path(__file__).resolve().parents[2]
    if options.validate_unattended_cycle:
        from qz_briefing.runtime.unattended_validation import print_unattended_validation, validate_unattended_cycle
        result = validate_unattended_cycle()
        print_unattended_validation(result)
        return 0 if result["success"] else 1
    if options.validate_stock_recommendations:
        from qz_briefing.recommendations.validation import print_stock_validation, validate_stock_recommendations
        result = validate_stock_recommendations()
        print_stock_validation(result)
        return 0 if result["success"] else 1
    if options.validate_recommendation_data_pipeline:
        from qz_briefing.recommendations.data_validation import print_recommendation_data_validation, validate_recommendation_data_pipeline
        result = validate_recommendation_data_pipeline()
        print_recommendation_data_validation(result)
        return 0 if result["success"] else 1
    if options.plan_live_recommendation_collection:
        from qz_briefing.recommendations.collection_orchestrator import print_plan_modes
        print_plan_modes(max_symbols=options.max_symbols)
        return 0
    if options.collect_recommendation_data:
        from qz_briefing.recommendations.collection_orchestrator import run_collection_dry_run
        try:
            return run_collection_dry_run(options.mode,max_symbols=options.max_symbols,dry_run=options.dry_run)
        except ValueError as exc:
            print(f"COLLECTION BLOCKED: {exc}")
            return 2
    if options.diagnose_opt10081_live:
        from qz_briefing.recommendations.opt10081_diagnostic import print_diagnostic, run_opt10081_diagnostic
        try:
            result = run_opt10081_diagnostic(project_root, options.symbol)
        except (RuntimeError, ValueError) as exc:
            print(f"LIVE DIAGNOSTIC FAILED: {exc}")
            return 2
        return 0 if print_diagnostic(result) else 1
    if options.diagnose_kiwoom_login:
        from qz_briefing.kiwoom.login_diagnostic import print_login_diagnostic, run_login_diagnostic
        result = run_login_diagnostic()
        return 0 if print_login_diagnostic(result) else 1
    if options.validate_live_recommendation_collection:
        from qz_briefing.recommendations.live_validation import print_live_summary, run_live_validation
        try:
            result = run_live_validation(project_root, max_symbols=options.max_symbols or 5, collect_flow=not options.price_only)
        except (RuntimeError, ValueError) as exc:
            print(f"LIVE RECOMMENDATION VALIDATION FAILED: {exc}")
            return 2
        return 0 if print_live_summary(result) else 1
    if options.diagnose_opt10059_live:
        from qz_briefing.recommendations.opt10059_diagnostic import print_opt10059_diagnostic, run_opt10059_diagnostic
        try:
            result=run_opt10059_diagnostic(project_root,options.symbol)
        except (RuntimeError,ValueError) as exc:
            print(f"OPT10059 LIVE DIAGNOSTIC FAILED: {exc}")
            return 2
        return 0 if print_opt10059_diagnostic(result) else 1
    if options.validate_cached_opt10059_candidates:
        from qz_briefing.recommendations.opt10059_diagnostic import run_cached_opt10059_candidates
        result=run_cached_opt10059_candidates(project_root)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0 if result["requested"] and result["failed"]==0 else 1
    cli_result = handle_notification_cli(options, project_root)
    if cli_result is not None:
        return cli_result
    manual_market_close = options.run_now == BriefingType.MARKET_CLOSE.value
    if manual_market_close:
        print("manual briefing requested: market_close", flush=True)
    process_lock = lock_factory()
    if not acquire_process_lock(process_lock):
        print("QZ BRIEFING ALREADY RUNNING", flush=True)
        return 2

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
        print("market calendar incomplete; continuing in warning mode", flush=True)
    if trading_day.status is MarketStatus.CLOSED and not manual_market_close:
        print("confirmed market closure; no runtime started", flush=True)
        process_lock.unlock()
        return 0

    shutdown_controller: GracefulShutdownController | None = None
    adapter: KiwoomQAxAdapter | None = None
    runtime: QtConnectionRuntime | None = None
    briefing_scheduler: BriefingScheduler | None = None
    sleep_inhibitor: SleepInhibitor | None = None
    try:
        application = application_factory(sys.argv if arguments is None else arguments)
        from PyQt5.QtCore import QTimer
        data_root = project_root / "data"
        logging_configurator(data_root)
        notification_service = notification_service_factory(project_root, data_root, QTimer)
        print(f"PROCESS PID: {os.getpid()}", flush=True)
        print("QAPPLICATION READY", flush=True)
        shutdown_controller = shutdown_controller_factory(application, process_lock)
        application.aboutToQuit.connect(shutdown_controller.handle_application_quit)
        if not shutdown_controller.schedule():
            return 0
        shutdown_controller.attach_briefing_scheduler(notification_service)
        sleep_inhibitor = sleep_inhibitor_factory()
        sleep_inhibitor.start()

        adapter = adapter_factory()
        print("KIWOOM OCX READY", flush=True)
        manager = manager_factory(adapter)
        tr_queue = tr_queue_factory(adapter)
        runtime_monitor = runtime_monitor_factory(
            data_root, timer_factory=QTimer, clock=clock,
            connection_state=lambda: manager.state.name,
            tr_progress=lambda: getattr(tr_queue, "progress", {}),
            watchdog_recover=lambda reason: manager.request_connection_recheck(reason),
        )
        shutdown_controller.attach_briefing_scheduler(runtime_monitor)
        shutdown_controller.attach_briefing_scheduler(sleep_inhibitor)
        if hasattr(tr_queue, "set_timeout_observer"):
            def observe_tr_timeout(count: int) -> None:
                if count < 2: return
                if hasattr(tr_queue, "pause"): tr_queue.pause("consecutive TR timeouts detected")
                manager.request_connection_recheck("consecutive TR timeouts detected")
            tr_queue.set_timeout_observer(observe_tr_timeout)
        shutdown_controller.attach_briefing_scheduler(tr_queue)
        pipeline = briefing_pipeline_factory(clock, tr_queue)
        if hasattr(pipeline, "add_completion_listener"):
            def notify_saved_briefing(name: str, path: str) -> None:
                try:
                    enqueue_saved_briefing(notification_service, name, path)
                except Exception:
                    logging.getLogger(__name__).exception("notification enqueue failed; briefing remains complete")
            pipeline.add_completion_listener(notify_saved_briefing)
        preopen_source = (
            KiwoomPreopenRealSource(adapter, ("005930", "000660"))
            if hasattr(adapter, "add_real_data_listener") else None
        )
        preopen_monitor = PreopenMonitoringController(
            preopen_source.snapshot if preopen_source else lambda: {
                "market_open_detected": False,
                "data_source": "not_available",
                "warnings": ["official real-time interface is unavailable"],
            }, clock=clock, timer_factory=QTimer
        )
        runtime_monitor.extra_status = lambda: {
            "sleep_prevention_active": sleep_inhibitor.active,
            "preopen_monitoring_status": preopen_monitor.result.get("coverage_status"),
            "preopen_sample_count": preopen_monitor.result.get("sample_count"),
            "telegram_enabled": notification_service.status.enabled,
            "telegram_configured": notification_service.status.configured,
            "telegram_last_success_at": notification_service.status.last_success_at,
            "telegram_last_event": notification_service.status.last_event,
            "telegram_pending_count": notification_service.status.pending_count,
            "telegram_last_error": notification_service.status.last_error,
            "telegram_next_attempt_at": notification_service.status.next_attempt_at,
        }
        runtime_monitor.summary_listener = lambda summary: notification_service.send_daily_summary and notification_service.submit(
            NotificationRequest(
                event_type="daily_summary", trading_date=clock().date().isoformat(),
                text=format_daily_summary(summary),
            )
        )
        if hasattr(pipeline, "set_preopen_monitoring_provider"):
            pipeline.set_preopen_monitoring_provider(lambda: preopen_monitor.result)
        shutdown_controller.attach_briefing_scheduler(preopen_monitor)
        if preopen_source is not None:
            shutdown_controller.attach_briefing_scheduler(preopen_source)
        preopen_requested = False
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
            preopen_monitor.refresh_market_state()
            if briefing_type is BriefingType.PRE_MARKET:
                preopen_monitor.stop()
                if preopen_source is not None:
                    preopen_source.stop()
            def execute() -> None:
                runtime_monitor.briefing_started(briefing_type.value)
                try:
                    pipeline.run(
                        briefing_type,
                        now.date(),
                        market_calendar_status=trading_day.status.value,
                        market_calendar_reason=trading_day.reason,
                        market_calendar_warning=trading_day.warning,
                    )
                except Exception:
                    if notification_service.send_runtime_alerts:
                        notification_service.submit(
                            NotificationRequest(
                                event_type="briefing_failed",
                                trading_date=now.date().isoformat(),
                                text=format_runtime_alert(
                                    f"{briefing_type.value} 브리핑 생성에 실패했습니다.",
                                    clock().strftime("%H:%M"),
                                ),
                            )
                        )
                    raise
            dispatcher.dispatch(
                now.date(),
                briefing_type,
                execute,
            )

        pre_market_grace_timer = None

        def request_pre_market() -> None:
            """Wait through 09:05 only when no official open/trade signal exists."""
            nonlocal pre_market_grace_timer
            current = clock()
            if preopen_monitor.refresh_market_state() or current.time() >= time(9, 5):
                run_briefing(BriefingType.PRE_MARKET)
                return
            grace_end = datetime.combine(current.date(), time(9, 5), tzinfo=current.tzinfo)
            pre_market_grace_timer = QTimer()
            pre_market_grace_timer.setSingleShot(True)
            pre_market_grace_timer.timeout.connect(
                lambda: run_briefing(BriefingType.PRE_MARKET)
            )
            pre_market_grace_timer.start(
                max(0, int((grace_end - current).total_seconds() * 1000))
            )
            shutdown_controller.attach_briefing_scheduler(pre_market_grace_timer)
            print("market open confirmation pending until 09:05", flush=True)

        def start_preopen_monitoring() -> None:
            nonlocal preopen_requested
            preopen_requested = True
            if manager.state is not ConnectionState.CONNECTED:
                print("preopen monitoring pending: Kiwoom is not connected", flush=True)
                return
            if preopen_source is not None:
                preopen_source.start()
            preopen_monitor.start()
            print("preopen monitoring started", flush=True)

        callbacks = {
                PREOPEN_MONITORING: lambda: start_preopen_monitoring(),
                BriefingType.PRE_MARKET.value: request_pre_market,
                BriefingType.INTRADAY_10AM.value: lambda: run_briefing(
                    BriefingType.INTRADAY_10AM
                ),
                BriefingType.MARKET_CLOSE.value: lambda: run_briefing(
                    BriefingType.MARKET_CLOSE
                ),
        }
        runtime_monitor.recovery = MissingBriefingRecovery(
            data_root, callbacks, clock=clock,
            running=lambda name: runtime_monitor.active_briefing == name,
            pending=lambda name: dispatcher.is_pending(
                clock().date(), BriefingType(name)
            ) if name in {item.value for item in BriefingType} else False,
        )
        if hasattr(pipeline, "add_completion_listener"):
            pipeline.add_completion_listener(
                lambda name, path: runtime_monitor.briefing_completed(name.split()[0])
            )
        if not manual_market_close:
            briefing_scheduler = briefing_scheduler_factory(callbacks)
        shutdown_controller.attach_briefing_scheduler(dispatcher)
        if briefing_scheduler is not None:
            shutdown_controller.attach_briefing_scheduler(briefing_scheduler)
        def report_connection_state(state: ConnectionState) -> None:
            nonlocal preopen_requested
            if state is ConnectionState.CONNECTED and hasattr(tr_queue, "resume"):
                tr_queue.resume()
            elif state in {ConnectionState.RECHECKING, ConnectionState.RECONNECT_WAIT, ConnectionState.RECONNECTING, ConnectionState.FAILED} and hasattr(tr_queue, "pause"):
                tr_queue.pause(f"connection recovery state: {state.name}")
            dispatcher.on_connection_state(state)
            if state is ConnectionState.CONNECTED and preopen_requested and not preopen_monitor.result.get("actual_start"):
                start_preopen_monitoring()
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
        runtime_monitor.start()
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
        if sleep_inhibitor is not None:
            sleep_inhibitor.stop()
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
