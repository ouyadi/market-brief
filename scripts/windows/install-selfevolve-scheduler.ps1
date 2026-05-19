# install-selfevolve-scheduler.ps1 -- register Phase 2a self-evolve scheduler.
#
# The task runs once per weekday evening after the last MarketBrief scan. The
# Python scheduler decides which self-evolve command to run for that weekday and
# only creates pending proposals; it never auto-applies changes.

$ErrorActionPreference = "Stop"

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $here 'run-selfevolve.ps1'
$vbs    = Join-Path $here 'run-hidden.vbs'

if (-not (Test-Path $runner)) { Write-Error "Cannot find $runner"; exit 1 }
if (-not (Test-Path $vbs))    { Write-Error "Cannot find $vbs (no-flash wrapper)"; exit 1 }

$taskName = "SelfEvolveScheduler"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$vbs`" `"powershell.exe`" `"-NoProfile`" `"-ExecutionPolicy`" `"Bypass`" `"-File`" `"$runner`""

# 23:10 local time: after the 22:00 MarketBrief task has had time to finish.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 11:10pm

$principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 55)

Register-ScheduledTask `
    -TaskName    $taskName `
    -Action      $action `
    -Trigger     $trigger `
    -Principal   $principal `
    -Settings    $settings `
    -Description "Phase 2a self-evolve scheduler: runs one review command nightly and creates pending proposals only." | Out-Null

Write-Host "Registered scheduled task '$taskName' (weekdays 23:10 local)." -ForegroundColor Green
Write-Host ""
Write-Host "Operate:"
Write-Host "  Start-ScheduledTask    -TaskName $taskName"
Write-Host "  Get-ScheduledTaskInfo  -TaskName $taskName"
Write-Host "  Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
Write-Host ""
Write-Host "Manual dry-run:"
Write-Host "  & `"$runner`" -DryRun"
