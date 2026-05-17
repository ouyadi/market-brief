# install-twitter-mcp.ps1 -- register TwitterMCP scheduled task.
#
# The MCP is a long-running Python process that:
#   * starts headless Chromium with the user's X cookies injected
#   * exposes HTTP MCP at 127.0.0.1:3031/mcp
#   * provides fetch_tweet_by_url / fetch_user_tweets / search_tweets
#
# At Log On, hidden window, stdout/stderr redirected to logs/.

$ErrorActionPreference = "Stop"

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy  = if ($env:HERMES_VENV) { Join-Path $env:HERMES_VENV 'Scripts\python.exe' }
           else { Join-Path $env:USERPROFILE 'hermes-agent\.venv\Scripts\python.exe' }
$script  = Join-Path $here 'twitter_playwright_mcp.py'
$vbs     = Join-Path $here 'run-hidden.vbs'
$logDir  = Join-Path $here 'logs'

if (-not (Test-Path $venvPy)) { Write-Error "venv python missing: $venvPy"; exit 2 }
if (-not (Test-Path $script)) { Write-Error "wrapper missing: $script"; exit 2 }
if (-not (Test-Path $vbs))    { Write-Error "no-flash wrapper missing: $vbs"; exit 2 }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

$taskName = "TwitterMCP"

$today  = (Get-Date).ToString('yyyy-MM-dd')
$logOut = Join-Path $logDir "twitter-mcp.$today.out.log"
$logErr = Join-Path $logDir "twitter-mcp.$today.err.log"

$wrapper = "Start-Process -FilePath '$venvPy' " +
           "-ArgumentList '$script' " +
           "-WorkingDirectory '$here' " +
           "-WindowStyle Hidden " +
           "-RedirectStandardOutput '$logOut' " +
           "-RedirectStandardError '$logErr'"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# wscript+VBS for no-flash launch; see run-hidden.vbs for rationale.
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
    -Description "Twitter/X HTTP MCP via Playwright headless Chromium on 127.0.0.1:3031" | Out-Null

Write-Host "Registered task '$taskName' (At log on)" -ForegroundColor Green
Write-Host ""
Write-Host "Operate:"
Write-Host "  Start-ScheduledTask   -TaskName $taskName"
Write-Host "  Get-ScheduledTaskInfo -TaskName $taskName"
Write-Host "  Stop-ScheduledTask    -TaskName $taskName"
Write-Host ""
Write-Host "Logs:"
Write-Host "  Get-Content '$logDir\twitter_mcp.log' -Tail 20 -Wait"
