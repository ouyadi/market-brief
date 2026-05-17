# Register two Windows scheduled tasks that start the local MCP servers at logon.
#
#   ChatlogServer   -- TUI app; needs a real console (use -WindowStyle Minimized
#                      WITHOUT stdio redirects, or the Ink UI bails out).
#                      chatlog.exe ends up as a minimized window in the taskbar.
#   DiscordSelfbot  -- plain Python service; safe to run -WindowStyle Hidden
#                      with stdio redirected to log files
#
# Both tasks themselves are launched via wscript.exe + run-hidden.vbs so the
# outer powershell wrapper never flashes a console window. See run-hidden.vbs
# header for the rationale.
#
# Both: At-log-on trigger, IgnoreNew so duplicate starts skip, restart on failure.

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs  = Join-Path $here 'run-hidden.vbs'
if (-not (Test-Path $vbs)) {
    Write-Error "Cannot find $vbs -- copy run-hidden.vbs into this dir from the repo"
    exit 1
}

function Register-McpTask {
    param(
        [string] $Name,
        [string] $Wrapper,          # the inline `Start-Process ...` command
        [string] $Description
    )
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue

    # wscript.exe is the windowless Windows Script Host -> never flashes.
    # run-hidden.vbs rebuilds the command line from its args (quote-wrapped)
    # and spawns it via WshShell.Run(cmd, 0, False) where 0=Hidden, so the
    # child powershell also never gets a console.
    $action  = New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "`"$vbs`" `"powershell.exe`" `"-NoProfile`" `"-ExecutionPolicy`" `"Bypass`" `"-Command`" `"$Wrapper`""

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
        -TaskName    $Name `
        -Action      $action `
        -Trigger     $trigger `
        -Principal   $principal `
        -Settings    $settings `
        -Description $Description | Out-Null

    Write-Host "Registered task '$Name' (At log on)" -ForegroundColor Green
}

# Ensure log dirs exist
foreach ($d in @(
    "C:\Users\ouyad\chatlog\logs",
    "C:\Users\ouyad\discord-selfbot-mcp\logs"
)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

# ChatlogServer: TUI app, Minimized window, no redirect.
$chatlogWrapper = "Start-Process -FilePath 'C:\Users\ouyad\chatlog\chatlog.exe' -WorkingDirectory 'C:\Users\ouyad\chatlog' -WindowStyle Minimized"
Register-McpTask `
    -Name "ChatlogServer" `
    -Wrapper $chatlogWrapper `
    -Description "Local chatlog HTTP+MCP server on 127.0.0.1:5030 (reads WeChat 4.x DB). TUI window minimized to taskbar."

# DiscordSelfbot: Python, fine with hidden + redirect.
$today = (Get-Date).ToString('yyyy-MM-dd')
$disLogOut = "C:\Users\ouyad\discord-selfbot-mcp\logs\$today.out.log"
$disLogErr = "C:\Users\ouyad\discord-selfbot-mcp\logs\$today.err.log"
$discordWrapper = "Start-Process -FilePath 'C:\Users\ouyad\discord-selfbot-mcp\.venv\Scripts\python.exe' -ArgumentList 'server.py' -WorkingDirectory 'C:\Users\ouyad\discord-selfbot-mcp' -WindowStyle Hidden -RedirectStandardOutput '$disLogOut' -RedirectStandardError '$disLogErr'"
Register-McpTask `
    -Name "DiscordSelfbot" `
    -Wrapper $discordWrapper `
    -Description "Local discord-selfbot MCP server on 127.0.0.1:6280 (uses burner Discord token)."

Write-Host ""
Write-Host "Both registered. Trigger now:"
Write-Host "  Start-ScheduledTask -TaskName ChatlogServer    # minimized to taskbar"
Write-Host "  Start-ScheduledTask -TaskName DiscordSelfbot   # fully hidden"
Write-Host ""
Write-Host "Verify both listening:"
Write-Host "  Get-NetTCPConnection -LocalPort 5030,6280 -State Listen"
