"""Static checks for the Windows Task Scheduler management scripts.

These tests only read PowerShell source files. They never execute the scripts or
call Windows Task Scheduler cmdlets.
"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "install_qz_briefing_task.ps1"
UNINSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "uninstall_qz_briefing_task.ps1"


def _read_script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_scheduler_scripts_exist() -> None:
    assert INSTALL_SCRIPT.is_file()
    assert UNINSTALL_SCRIPT.is_file()


def test_task_name_is_declared_in_both_scripts() -> None:
    expected = '$TaskName = "QZ_Briefing_AutoStart"'
    assert expected in _read_script(INSTALL_SCRIPT)
    assert expected in _read_script(UNINSTALL_SCRIPT)


def test_install_script_uses_daily_730_am_trigger() -> None:
    content = _read_script(INSTALL_SCRIPT)
    assert "New-ScheduledTaskTrigger -Daily -At \"07:30\"" in content
    assert "-MultipleInstances IgnoreNew" in content
    assert "-WakeToRun" in content


def test_install_script_uses_project_virtualenv_python() -> None:
    content = _read_script(INSTALL_SCRIPT)
    assert '$PythonPath = "D:\\QZ_Briefing\\.venv\\Scripts\\python.exe"' in content
    assert "-Execute $PythonPath" in content


def test_install_script_uses_module_arguments_and_working_directory() -> None:
    content = _read_script(INSTALL_SCRIPT)
    assert '$PythonArguments = "-m qz_briefing"' in content
    assert "-Argument $PythonArguments" in content
    assert '$ProjectPath = "D:\\QZ_Briefing"' in content
    assert "-WorkingDirectory $ProjectPath" in content


def test_install_script_uses_current_user_interactive_principal() -> None:
    content = _read_script(INSTALL_SCRIPT)
    assert "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name" in content
    assert "-UserId $CurrentUser" in content
    assert "-LogonType Interactive" in content


def test_install_script_does_not_block_on_network_availability() -> None:
    assert "-RunOnlyIfNetworkAvailable" not in _read_script(INSTALL_SCRIPT)


def test_install_script_allows_battery_operation() -> None:
    content = _read_script(INSTALL_SCRIPT)
    assert "-AllowStartIfOnBatteries" in content
    assert "-DontStopIfGoingOnBatteries" in content


def test_install_script_starts_after_missed_schedule() -> None:
    assert "-StartWhenAvailable" in _read_script(INSTALL_SCRIPT)


def test_install_script_registers_idempotently() -> None:
    content = _read_script(INSTALL_SCRIPT)
    assert "Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue" in content
    assert "Register-ScheduledTask" in content
    assert "-Force" in content


def test_uninstall_script_checks_before_removal() -> None:
    content = _read_script(UNINSTALL_SCRIPT)
    assert "Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue" in content
    assert "if ($null -ne $ExistingTask)" in content
    assert "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false" in content


def test_scripts_report_task_name_and_result() -> None:
    install_content = _read_script(INSTALL_SCRIPT)
    uninstall_content = _read_script(UNINSTALL_SCRIPT)
    assert install_content.count("Write-Host \"[$TaskName]") == 2
    assert uninstall_content.count("Write-Host \"[$TaskName]") == 2
