# One-time setup: register the market-brief task with Windows Task Scheduler.
# Fires hourly from 08:00 to 22:00 local time (15 runs/day).
# Set your Windows timezone to America/New_York for EDT alignment.

$taskName = "MarketBrief"
$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner   = Join-Path $here "run.ps1"

if (-not (Test-Path $runner)) {
    Write-Error "Cannot find $runner"
    exit 1
}

# Remove existing task with same name (idempotent re-install)
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""

# One daily trigger at 08:00 that then repeats every hour for 14 hours
# => fires at 08, 09, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22 (15 total)
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00am
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 8:00am `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Hours 14)).Repetition

$principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Limited

# IgnoreNew = if a previous run is still going when the next hour fires, skip it
# (avoids piling up concurrent claude --print processes if a scan runs long)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 50)

Register-ScheduledTask `
    -TaskName    $taskName `
    -Action      $action `
    -Trigger     $trigger `
    -Principal   $principal `
    -Settings    $settings `
    -Description "Hourly 08:00-22:00 chat intel scan -- Discord + WeChat investing groups, emails a tier-tagged summary." | Out-Null

Write-Host "Installed scheduled task '$taskName'. Fires hourly 08:00 - 22:00 local time (15 runs/day)." -ForegroundColor Green
Write-Host ""
Write-Host "Trigger now (without waiting):  Start-ScheduledTask -TaskName $taskName"
Write-Host "Status:                          Get-ScheduledTaskInfo -TaskName $taskName"
Write-Host "Manual test (dry-run):           & '$runner' -SkipEmail"
