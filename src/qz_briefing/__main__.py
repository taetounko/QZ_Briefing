"""Executable QApplication entry point for QZ Briefing."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
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


ApplicationFactory = Callable[[Sequence[str]], ApplicationLike]
AdapterFactory = Callable[[], KiwoomQAxAdapter]
ManagerFactory = Callable[[KiwoomQAxAdapter], KiwoomConnectionManager]
RuntimeFactory = Callable[..., QtConnectionRuntime]


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

    def __init__(self, manager: KiwoomConnectionManager) -> None:
        self._manager = manager
        self._reported_transition_count = 0

    def __call__(self, runtime: QtConnectionRuntime) -> None:
        del runtime
        transitions = self._manager.transitions
        for transition in transitions[self._reported_transition_count :]:
            self._report_transition(transition)
        self._reported_transition_count = len(transitions)

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
        elif transition.reason == "login event reported an error":
            print("LOGIN FAILED", flush=True)


def run(
    arguments: Sequence[str] | None = None,
    *,
    application_factory: ApplicationFactory = create_application,
    adapter_factory: AdapterFactory = KiwoomQAxAdapter,
    manager_factory: ManagerFactory = KiwoomConnectionManager,
    runtime_factory: RuntimeFactory = QtConnectionRuntime,
) -> int:
    """Assemble the connection runtime and keep the Qt event loop running."""
    application = application_factory(sys.argv if arguments is None else arguments)
    print("QAPPLICATION READY", flush=True)
    adapter = adapter_factory()
    print("KIWOOM OCX READY", flush=True)
    runtime: QtConnectionRuntime | None = None
    try:
        manager = manager_factory(adapter)
        reporter = ConsoleConnectionReporter(manager)
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
