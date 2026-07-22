$ErrorActionPreference = "Stop"

$TaskName = "QZ_Briefing_AutoStart"
$ProjectPath = "D:\QZ_Briefing"
$PythonPath = "D:\QZ_Briefing\.venv\Scripts\python.exe"
$PythonArguments = "-m qz_briefing"

if (-not (Test-Path -LiteralPath $ProjectPath -PathType Container)) {
    throw "Project directory does not exist: $ProjectPath"
}

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable does not exist: $PythonPath"
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $PythonArguments `
    -WorkingDirectory $ProjectPath

$Trigger = New-ScheduledTaskTrigger -Daily -At "07:30"

# Kiwoom OpenAPI/QAxWidget requires the signed-in user's interactive desktop.
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Start QZ Briefing daily in the signed-in user's interactive session." `
    -Force | Out-Null

if ($null -eq $ExistingTask) {
    Write-Host "[$TaskName] scheduled task was installed successfully."
}
else {
    Write-Host "[$TaskName] scheduled task was updated successfully."
}
