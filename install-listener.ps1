# install-listener.ps1 -- register the WeixinListener scheduled task.
#
# The listener is a long-running Python process that:
#   1. long-polls iLink for inbound WeChat messages from the bot owner
#   2. dispatches each message to `claude --print` (or built-in commands)
#   3. pushes the reply back via Hermes' send_weixin_direct
#
# Same shape as chat-mcp-setup's DiscordSelfbot task:
#   - At Log On trigger
#   - Hidden window (plain Python, no TUI)
#   - stdout/stderr redirected to logs/listener.*.log
#   - Restart on failure (3x with 1 min backoff)
#   - IgnoreNew so a second logon doesn't double-start

$ErrorActionPreference = "Stop"

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $here 'run-listener.ps1'
$vbs    = Join-Path $here 'run-hidden.vbs'
$logDir = Join-Path $here 'logs'

if (-not (Test-Path $runner)) { Write-Error "Cannot find $runner"; exit 1 }
if (-not (Test-Path $vbs))    { Write-Error "Cannot find $vbs (no-flash wrapper)"; exit 1 }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

$taskName = "WeixinListener"

# Compose the inner wrapper that Start-Processes the listener detached with
# stdout/stderr redirected to log files. This whole thing runs invisibly
# inside the wscript+VBS launcher so neither the outer host nor the
# spawned powershell flash a console.
$today    = (Get-Date).ToString('yyyy-MM-dd')
$logOut   = Join-Path $logDir "listener.$today.out.log"
$logErr   = Join-Path $logDir "listener.$today.err.log"

$wrapper = "Start-Process -FilePath 'powershell.exe' " +
           "-ArgumentList '-NoProfile -ExecutionPolicy Bypass -File `"$runner`"' " +
           "-WindowStyle Hidden " +
           "-RedirectStandardOutput '$logOut' " +
           "-RedirectStandardError '$logErr'"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# wscript.exe + run-hidden.vbs -> truly no-flash launcher. See run-hidden.vbs
# header for why this is necessary instead of just powershell -WindowStyle Hidden.
$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$vbs`" `"powershell.exe`" `"-NoProfile`" `"-ExecutionPolicy`" `"Bypass`" `"-Command`" `"$wrapper`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Limited

Register-ScheduledTask `
    -TaskName    $taskName `
    -Action      $action `
    -Trigger     $trigger `
    -Principal   $principal `
    -Settings    $settings `
    -Description "Inbound WeChat -> Claude listener (iLink long-poll). Replies via Hermes Agent send_weixin_direct." | Out-Null

Write-Host "Registered scheduled task '$taskName' (At log on)." -ForegroundColor Green
Write-Host ""
Write-Host "Operate:"
Write-Host "  Start-ScheduledTask    -TaskName $taskName    # trigger now"
Write-Host "  Get-ScheduledTaskInfo  -TaskName $taskName    # state + last result"
Write-Host "  Stop-ScheduledTask     -TaskName $taskName    # stop"
Write-Host "  Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false   # remove"
Write-Host ""
Write-Host "Live logs:"
Write-Host "  Get-Content `"$logDir\listen_weixin.log`" -Tail 20 -Wait"
