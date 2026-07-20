$ErrorActionPreference = "Stop"

$TaskName = "QZ_Briefing_AutoStart"
$ProjectPath = "D:\QZ_Briefing"
$PythonPath = "D:\QZ_Briefing\.venv\Scripts\python.exe"

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($null -ne $ExistingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[$TaskName] scheduled task was removed successfully."
}
else {
    Write-Host "[$TaskName] scheduled task is not installed; nothing to remove."
}
