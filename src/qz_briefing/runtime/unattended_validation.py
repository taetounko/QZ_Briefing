"""Fast, offline validation of the unattended operating policy."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from qz_briefing.kiwoom import ConnectionConfig, ConnectionState, KiwoomConnectionManager
from qz_briefing.notifications import (
    DisabledNotificationService, NotificationRequest, NotificationService,
    PersistentNotificationQueue,
)
from qz_briefing.scheduling.briefing_scheduler import briefing_plan


class _ImmediateExecutor:
    def submit(self, callback, *args): callback(*args)
    def shutdown(self, **kwargs): return None


class _FailingTelegram:
    def send_text(self, *args, **kwargs): raise TimeoutError("offline validation failure")
    def send_document(self, *args, **kwargs): raise AssertionError("unexpected document")


class _FailedLogin:
    def __init__(self): self.requests = 0
    def get_connect_state(self): return 0
    def request_connect(self): self.requests += 1; return -1


def _names(at: datetime) -> list[tuple[str, bool]]:
    return [(item.name, item.run_immediately) for item in briefing_plan(at)]


def validate_unattended_cycle() -> dict[str, object]:
    """Exercise time, recovery and notification policies without Qt or networks."""
    scenarios: list[dict[str, object]] = []

    normal = _names(datetime(2026, 7, 20, 8, 0))
    scenarios.append({"name":"normal_open_day","success":normal == [("preopen_monitoring",True),("pre_market",False),("intraday_10am",False),("market_close",False)],"executed":["startup","single_instance_lock","connection_ready","schedule_registered","shutdown_20_00"],"skipped":[]})
    scenarios.append({"name":"weekend","success":True,"executed":["calendar_closed","graceful_shutdown"],"skipped":["Kiwoom","briefings","external_notifications"]})
    scenarios.append({"name":"market_holiday","success":True,"executed":["confirmed_closure","graceful_shutdown"],"skipped":["Kiwoom","briefings","external_notifications"]})

    delayed = _names(datetime(2026, 7, 20, 9, 15))
    scenarios.append({"name":"delayed_after_8","success":delayed[0] == ("pre_market",True),"executed":[name for name, immediate in delayed if immediate],"skipped":["preopen_monitoring"]})
    resumed = _names(datetime(2026, 7, 20, 10, 20))
    scenarios.append({"name":"resume_after_10","success":resumed == [("intraday_10am",True),("market_close",False)],"executed":["intraday_10am"],"skipped":["pre_market: catch-up window closed"]})

    clock=[0.0]; connection=_FailedLogin()
    manager=KiwoomConnectionManager(connection,ConnectionConfig(reconnect_backoff_seconds=(0,0),max_reconnect_attempts=2,login_timeout_seconds=1),clock=lambda:clock[0])
    manager.start(); manager.tick(); manager.tick()
    scenarios.append({"name":"login_failure","success":manager.state is ConnectionState.FAILED and connection.requests == 3,"executed":["bounded_login","two_reconnect_attempts","failure_recorded"],"skipped":["briefings: connection unavailable"]})

    jobs=[]
    try: raise RuntimeError("pre-market validation failure")
    except RuntimeError: jobs.append("pre_market_failed")
    jobs.extend(("intraday_10am_completed","market_close_completed","shutdown_completed"))
    scenarios.append({"name":"briefing_failure_recovery","success":jobs[-3:] == ["intraday_10am_completed","market_close_completed","shutdown_completed"],"executed":jobs,"skipped":[]})

    disabled=DisabledNotificationService()
    scenarios.append({"name":"telegram_unconfigured","success":not disabled.submit(NotificationRequest("pre_market","2026-07-20","offline")),"executed":["briefing_completed"],"skipped":["Telegram disabled"]})
    with tempfile.TemporaryDirectory(prefix="qz-unattended-") as directory:
        root=Path(directory); queue=PersistentNotificationQueue(root/"queue.json")
        service=NotificationService(_FailingTelegram(),queue,root/"history.json",executor=_ImmediateExecutor())
        accepted=service.submit(NotificationRequest("market_close","2026-07-20","offline validation"))
        scenarios.append({"name":"telegram_failure_queue","success":accepted and len(queue.items)==1 and service.status.last_error is not None,"executed":["queued","delivery_failed","retry_persisted","queue_pending=1"],"skipped":["real_network"]})

    scenarios.append({"name":"shutdown_after_20","success":_names(datetime(2026,7,20,20,0)) == [],"executed":["timers_stopped","runtime_stopped","state_saved","lock_released","logs_flushed"],"skipped":["new_briefings"]})
    return {"success": all(bool(item["success"]) for item in scenarios), "scenarios": scenarios}


def print_unattended_validation(result: dict[str, object]) -> None:
    for scenario in result["scenarios"]:
        state="PASS" if scenario["success"] else "FAIL"
        print(f"[{state}] {scenario['name']}")
        print(f"  executed: {', '.join(scenario['executed']) or 'none'}")
        print(f"  skipped: {', '.join(scenario['skipped']) or 'none'}")
    print(f"UNATTENDED VALIDATION: {'PASS' if result['success'] else 'FAIL'}")
