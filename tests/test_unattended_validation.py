from qz_briefing.__main__ import parse_cli_arguments, run
from qz_briefing.runtime.unattended_validation import validate_unattended_cycle


def test_unattended_validation_covers_required_offline_scenarios():
    result=validate_unattended_cycle()
    scenarios={item["name"]:item for item in result["scenarios"]}
    assert result["success"]
    assert set(scenarios)=={
        "normal_open_day","weekend","market_holiday","delayed_after_8",
        "resume_after_10","login_failure","briefing_failure_recovery",
        "telegram_unconfigured","telegram_failure_queue","shutdown_after_20",
    }
    assert scenarios["resume_after_10"]["skipped"]==["pre_market: catch-up window closed"]
    assert "retry_persisted" in scenarios["telegram_failure_queue"]["executed"]


def test_validation_cli_runs_before_qt_lock_or_external_services(capsys):
    assert parse_cli_arguments(["--validate-unattended-cycle"]).validate_unattended_cycle
    def forbidden(*args,**kwargs): raise AssertionError("external runtime must not start")
    assert run(
        ["--validate-unattended-cycle"], application_factory=forbidden,
        adapter_factory=forbidden, lock_factory=forbidden,
        notification_service_factory=forbidden,
    )==0
    output=capsys.readouterr().out
    assert "UNATTENDED VALIDATION: PASS" in output
    assert "account" not in output.lower() and "token" not in output.lower()
