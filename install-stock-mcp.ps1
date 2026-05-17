# install-stock-mcp.ps1 -- register StockPriceMCP scheduled task (At log on).
#
# The MCP is a yfinance-backed HTTP server exposing get_quote / get_history /
# get_info / check_post_hoc. Listens on 127.0.0.1:3032/mcp.
#
# Same shape as TwitterMCP / WeixinListener: hidden window, stdout/stderr
# redirected to logs/, At-log-on trigger, IgnoreNew, RestartCount 3.

$ErrorActionPreference = "Stop"

$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPy  = 'C:\Users\ouyad\hermes-agent\.venv\Scripts\python.exe'
$script  = Join-Path $here 'stock_price_mcp.py'
$logDir  = Join-Path $here 'logs'

if (-not (Test-Path $venvPy)) { Write-Error "venv python missing: $venvPy"; exit 2 }
if (-not (Test-Path $script)) { Write-Error "wrapper missing: $script"; exit 2 }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

$taskName = "StockPriceMCP"

$today  = (Get-Date).ToString('yyyy-MM-dd')
$logOut = Join-Path $logDir "stock-mcp.$today.out.log"
$logErr = Join-Path $logDir "stock-mcp.$today.err.log"

$wrapper = "Start-Process -FilePath '$venvPy' " +
           "-ArgumentList '$script' " +
           "-WorkingDirectory '$here' " +
           "-WindowStyle Hidden " +
           "-RedirectStandardOutput '$logOut' " +
           "-RedirectStandardError '$logErr'"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$wrapper`""

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
    -Description "Stock price MCP (yfinance) on 127.0.0.1:3032 -- quote/history/info/check_post_hoc" | Out-Null

Write-Host "Registered task '$taskName' (At log on)" -ForegroundColor Green
Write-Host ""
Write-Host "Operate:"
Write-Host "  Start-ScheduledTask   -TaskName $taskName"
Write-Host "  Get-ScheduledTaskInfo -TaskName $taskName"
Write-Host "  Stop-ScheduledTask    -TaskName $taskName"
Write-Host ""
Write-Host "Logs:"
Write-Host "  Get-Content '$logDir\stock_mcp.log' -Tail 20 -Wait"
