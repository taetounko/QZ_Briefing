"""Executable QApplication entry point for QZ Briefing."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from qz_briefing.kiwoom import (
    ConnectionTransition,
    KiwoomConnectionManager,
    KiwoomQAxAdapter,
)
from qz_briefing.runtime import QtConnectionRuntime


class SignalLike(Protocol):
    def connect(self, callback: Callable[[], None]) -> None: ...


class ApplicationLike(Protocol):
    aboutToQuit: SignalLike

    def exec_(self) -> int: ...


class ProcessLockLike(Protocol):
    def tryLock(self, timeout: int = 0) -> bool: ...

    def removeStaleLockFile(self) -> bool: ...

    def unlock(self) -> None: ...


ApplicationFactory = Callable[[Sequence[str]], ApplicationLike]
AdapterFactory = Callable[[], KiwoomQAxAdapter]
ManagerFactory = Callable[[KiwoomQAxAdapter], KiwoomConnectionManager]
RuntimeFactory = Callable[..., QtConnectionRuntime]
LockFactory = Callable[[], ProcessLockLike]


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
    ) -> None:
        self._manager = manager
        self._adapter = adapter
        self._reported_transition_count = 0
        self._reported_connect_request_count = -1
        self._reported_login_event_count = 0

    def __call__(self, runtime: QtConnectionRuntime) -> None:
        del runtime
        transitions = self._manager.transitions
        for transition in transitions[self._reported_transition_count :]:
            self._report_transition(transition)
        self._reported_transition_count = len(transitions)
        self._report_adapter_diagnostics()

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
) -> int:
    """Assemble the connection runtime and keep the Qt event loop running."""
    process_lock = lock_factory()
    if not acquire_process_lock(process_lock):
        print("QZ BRIEFING ALREADY RUNNING", flush=True)
        return 2

    try:
        application = application_factory(sys.argv if arguments is None else arguments)
        print(f"PROCESS PID: {os.getpid()}", flush=True)
        print("QAPPLICATION READY", flush=True)
        adapter = adapter_factory()
        print("KIWOOM OCX READY", flush=True)
        runtime: QtConnectionRuntime | None = None
        try:
            manager = manager_factory(adapter)
            reporter = ConsoleConnectionReporter(manager, adapter)
            runtime = runtime_factory(adapter, manager, on_state_change=reporter)
            application.aboutToQuit.connect(runtime.stop)
            if not runtime.start():
                raise RuntimeError("Kiwoom connection runtime did not start")
            print("RUNTIME STARTED", flush=True)
            return int(application.exec_())
        finally:
            if runtime is None:
                adapter.close()
            else:
                runtime.stop()
            print("RUNTIME STOPPED", flush=True)
    finally:
        process_lock.unlock()


def main() -> int:
    """Run QZ Briefing and report startup failures without exposing credentials."""
    try:
        return run()
    except KeyboardInterrupt:
        print("INTERRUPTED", file=sys.stderr, flush=True)
        return 130
    except Exception as exc:
        print(
            f"STARTUP FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
