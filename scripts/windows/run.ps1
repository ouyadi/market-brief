# Market brief runner -- invoked by Windows Task Scheduler hourly 08:00-22:00 EDT.
#
# Pipeline:
#   1) run the configured LLM backend with prompt.md (Codex by default)
#   2) read produced report
#   3) push to WeChat via Hermes iLink   (primary channel)
#   4) if WeChat push failed, send email (fallback / backup channel)
#
# Flags:
#   -SkipEmail   never send the email fallback even if WeChat push fails
#                (useful for manual smoke tests where you don't want noise)

param([switch]$SkipEmail)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = if ($env:MARKET_BRIEF_LLM_BACKEND) { $env:MARKET_BRIEF_LLM_BACKEND.ToLowerInvariant() } else { "codex" }
$env:MARKET_BRIEF_LLM_BACKEND = $backend
$env:MARKET_BRIEF_DIR = $here

# --- 1. Resolve paths ----------------------------------------------------
$promptFile  = Join-Path $here "prompt.md"
$secretsFile = Join-Path $here "secrets.json"
$logDir      = Join-Path $here "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

# crude EDT (UTC-4) -- swap to -5 in Nov when DST ends, or use a proper TZ lookup.
# Add 30s slop so a task that fires a second or two early (Windows clock skew)
# still resolves to the intended hour. E.g. trigger at 14:59:58 should map to
# hour=15, not hour=14.
$nowEdt     = (Get-Date).ToUniversalTime().AddHours(-4).AddSeconds(30)
$date       = $nowEdt.ToString("yyyy-MM-dd")
$hour       = $nowEdt.ToString("HH")
$reportFile = Join-Path $env:USERPROFILE "Reports\$date-$hour-brief.md"
# Mirror what listen_weixin.py does -- ensure Reports/ exists so any user (not
# just the original author) gets the directory auto-created on first run.
$reportsDir = Join-Path $env:USERPROFILE "Reports"
if (-not (Test-Path $reportsDir)) { New-Item -ItemType Directory -Force -Path $reportsDir | Out-Null }
$logFile    = Join-Path $logDir "$date.log"

# Logging helper. Tee-Object holds a file handle for the whole pipeline and
# clashes with subsequent Add-Content calls on PS 5.1, so we use a single
# StreamWriter-free path that opens-appends-closes each line.
function Log {
    param([Parameter(ValueFromRemainingArguments)] $msg)
    $line = ($msg -join " ")
    # Force UTF-8 to stay compatible with whichever PS version writes; old files
    # written by Tee-Object as UTF-16 LE are rotated aside on first call below.
    try { Add-Content -Path $logFile -Value $line -Encoding UTF8 -ErrorAction Stop } catch { }
    Write-Host $line
}

function Log-CodexOutput {
    param(
        [object[]] $Lines,
        [int] $ExitCode,
        [string] $LastMessageFile
    )
    $lineArray = @($Lines | ForEach-Object { [string]$_ })
    if ($ExitCode -ne 0) {
        Log "    [codex] exited $ExitCode; output tail follows"
        foreach ($line in ($lineArray | Select-Object -Last 120)) {
            Log "    [codex] $line"
        }
        return
    }

    $warningCount = @($lineArray | Where-Object { $_ -match '\bWARN\b|warning|failed to load plugin|Cloudflare|challenge' }).Count
    $reportLines = @($lineArray | Where-Object {
        $_ -match '^\s*REPORT_WRITTEN:\s+' -or
        $_ -match '^\s*tokens used\b' -or
        $_ -match '^\s*ERROR:'
    })
    foreach ($line in $reportLines) {
        Log "    [codex] $line"
    }
    if ($warningCount -gt 0) {
        Log "    [codex] suppressed $warningCount startup warning/log line(s)"
    }
    if (Test-Path $LastMessageFile) {
        foreach ($line in (Get-Content -Encoding UTF8 $LastMessageFile)) {
            Log "    [codex-final] $line"
        }
    }
}

# Rotate out any pre-existing log file that's UTF-16 (from older Tee-Object
# code) -- mixing encodings makes the log unreadable.
if (Test-Path $logFile) {
    try {
        $head = [System.IO.File]::ReadAllBytes($logFile) | Select-Object -First 2
        if ($head.Count -ge 2 -and $head[0] -eq 0xFF -and $head[1] -eq 0xFE) {
            Move-Item -Path $logFile -Destination "$logFile.utf16.bak" -Force -ErrorAction SilentlyContinue
        }
    } catch { }
}

# Keep one active brief run per install. Task Scheduler is configured with
# MultipleInstances=IgnoreNew, but that only works if the scheduled action
# stays alive for the whole child process. The lock also protects manual smoke
# tests from overlapping an hourly run.
$lockFile = Join-Path $logDir "market-brief.lock"
try {
    $script:briefLock = [System.IO.File]::Open(
        $lockFile,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
    $lockText = "pid=$PID started=$([DateTime]::Now.ToString('o')) backend=$backend"
    $lockBytes = [System.Text.Encoding]::UTF8.GetBytes($lockText)
    $script:briefLock.SetLength(0)
    $script:briefLock.Write($lockBytes, 0, $lockBytes.Length)
    $script:briefLock.Flush()
} catch {
    Log "[WARN] another market-brief run appears active; skipping this tick. $($_.Exception.Message)"
    exit 0
}

# Tell the LLM child where to write -- avoids hour-boundary skew between
# run.ps1 launch time and the moment it calls current_time inside.
$env:MARKET_BRIEF_OUTPUT = $reportFile

# Pick a label for the email subject based on the trading-session bucket.
$hourInt = [int]$hour
if     ($hourInt -lt 9)  { $sessionTag = "pre-market"  }
elseif ($hourInt -le 16) { $sessionTag = "market"      }
else                     { $sessionTag = "after-hours" }

Log "[$([DateTime]::Now)] ==== market-brief run start (hour=$hour, session=$sessionTag) ===="

# --- env snapshot for forensics ------------------------------------------
try {
    Log "--- env snapshot (LLM/CODEX/CLAUDE/ANTHROPIC/API) ---"
    foreach ($e in (Get-ChildItem env:)) {
        if ($e.Name -notmatch 'MARKET_BRIEF_LLM|CODEX|CLAUDE|ANTHROPIC|API') { continue }
        $name = $e.Name
        $val  = $e.Value
        if ($name -match 'TOKEN|KEY|PASSWORD|SECRET') {
            $len = if ($val) { ([string]$val).Length } else { 0 }
            Log "    $name = <hidden, $len chars>"
        } else {
            Log "    $name = $val"
        }
    }
    Log "--- env snapshot end ---"
} catch {
    Log "[env dump warning] $($_.Exception.Message)"
}

# Strip everything Claude Desktop or some other parent process might have
# injected. The npm-installed claude CLI must talk to the public API directly;
# any host-managed shim env var causes opaque 405 errors.
@(
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_BEDROCK',
    'ANTHROPIC_VERTEX_PROJECT_ID',
    'ANTHROPIC_VERTEX_REGION',
    'CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST',
    'CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH',
    'CLAUDE_CODE_ENTRYPOINT',
    'CLAUDE_CODE_SESSION_ID',
    'CLAUDE_CODE_DISABLE_CRON',
    'CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES',
    'CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL',
    'CLAUDE_AGENT_SDK_VERSION',
    'CLAUDECODE'
) | ForEach-Object { Remove-Item "Env:$_" -ErrorAction SilentlyContinue }

# --- 2. Sanity -----------------------------------------------------------
if (-not (Test-Path $secretsFile)) {
    Log "[ERROR] secrets.json missing. Copy secrets.example.json to secrets.json and fill it in."
    exit 2
}
$secrets = Get-Content -Raw $secretsFile | ConvertFrom-Json

if ($backend -eq "claude") {
    if (-not $secrets.claudeCodeOauthToken) {
        Log "[ERROR] secrets.json missing claudeCodeOauthToken. Generate one with: claude setup-token"
        exit 2
    }
    $env:CLAUDE_CODE_OAUTH_TOKEN = $secrets.claudeCodeOauthToken
} else {
    Remove-Item "Env:CLAUDE_CODE_OAUTH_TOKEN" -ErrorAction SilentlyContinue
}

# --- 3. Run LLM backend --------------------------------------------------
# Prepend persistent memory (cross-run user angles, signal priorities,
# corrections that should outlive any single prompt.md edit). If
# memory.md exists, its content is wrapped in a clear separator block
# and put BEFORE prompt.md content. The listener writes user-confirmed
# durable feedback into this file in addition to prompt.md.
$memoryFile = Join-Path $here "memory.md"
$prompt = Get-Content -Raw -Encoding UTF8 $promptFile
if (Test-Path $memoryFile) {
    $memory = Get-Content -Raw -Encoding UTF8 $memoryFile
    $prompt = "<!-- PERSISTENT MEMORY (from memory.md) -->`n" +
              $memory + "`n" +
              "<!-- END PERSISTENT MEMORY -->`n`n" +
              $prompt
    Log "[$([DateTime]::Now)] prepended memory.md ($($memory.Length) chars)"
}
Log "[$([DateTime]::Now)] launching $backend (this may take a few minutes)..."

# Pipe prompt via stdin to avoid quoting hell.
# Dangerous permissions: required because the LAN MCP servers and
#   WebFetch would otherwise prompt and block; this script runs unattended.
$llmExit = 0
if ($backend -eq "codex" -or $backend -eq "gpt" -or $backend -eq "openai") {
    $lastMessageFile = Join-Path $logDir "codex-last-message-$date-$hour-$PID.txt"
    Remove-Item $lastMessageFile -ErrorAction SilentlyContinue
    $codexArgs = @(
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C", $here,
        "--output-last-message", $lastMessageFile,
        "--color", "never"
    )
    $codexModel = if ($env:MARKET_BRIEF_CODEX_MODEL) { $env:MARKET_BRIEF_CODEX_MODEL } else { "gpt-5.4" }
    $codexArgs += @("--model", $codexModel)
    Log "[$([DateTime]::Now)] codex model: $codexModel"
    $codexArgs += "-"
    # Native commands that write stderr can raise NativeCommandError under
    # $ErrorActionPreference=Stop on Windows PowerShell 5.1, even when the
    # process itself would continue normally. Capture stdout/stderr and trust
    # the exit code instead so scheduled runs do not disappear after launch.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $llmOut = $prompt | & codex @codexArgs 2>&1
        $llmExit = $LASTEXITCODE
    } catch {
        $llmOut = @("PowerShell caught codex exception: $($_.Exception.Message)")
        $llmExit = 1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    Log-CodexOutput -Lines $llmOut -ExitCode $llmExit -LastMessageFile $lastMessageFile
    Remove-Item $lastMessageFile -ErrorAction SilentlyContinue
} elseif ($backend -eq "claude") {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $llmOut = $prompt | & claude `
            --print `
            --dangerously-skip-permissions `
            --output-format text `
            2>&1
        $llmExit = $LASTEXITCODE
    } catch {
        $llmOut = @("PowerShell caught claude exception: $($_.Exception.Message)")
        $llmExit = 1
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    if ($llmOut) {
        foreach ($line in $llmOut) { Log "    [claude] $line" }
    }
} else {
    Log "[ERROR] unsupported MARKET_BRIEF_LLM_BACKEND=$backend (expected codex or claude)"
    exit 2
}

if ($llmExit -ne 0) {
    Log "[ERROR] $backend exited $llmExit. Aborting email."
    exit 3
}

# --- 4. Verify the report exists -----------------------------------------
if (-not (Test-Path $reportFile)) {
    Log "[ERROR] Expected report file not found: $reportFile"
    Log "[ERROR] See $backend stdout/stderr above. Aborting email."
    exit 3
}
Log "[$([DateTime]::Now)] report ready: $reportFile"

# --- 5. Push to WeChat via Hermes Agent iLink (primary channel) --------
# Allow power users to point at a non-default venv via $env:HERMES_VENV.
$venvPy   = if ($env:HERMES_VENV) { Join-Path $env:HERMES_VENV 'Scripts\python.exe' }
            else { Join-Path $env:USERPROFILE 'hermes-agent\.venv\Scripts\python.exe' }
$pushTool = Join-Path $here 'push_weixin.py'
$imageTool = Join-Path $here 'brief_image.py'
$wxPushOk = $false
$wechatMode = if ($env:MARKET_BRIEF_WECHAT_MODE) { $env:MARKET_BRIEF_WECHAT_MODE.ToLowerInvariant() } else { "image" }

if ((Test-Path $venvPy) -and (Test-Path $pushTool)) {
    # IMPORTANT: This script's top-level $ErrorActionPreference is "Stop",
    # which makes PowerShell raise an exception whenever a native command
    # writes ANY text to stderr. Hermes' Python logger emits stderr WARNINGs
    # during its normal retry-on-rate-limit flow ("backing off 3.0s before
    # retry"), and previously $EAP=Stop killed the push subprocess before
    # Hermes finished its retry, every brief falling through to email.
    # Locally relax $EAP for the push call -- only the exit code matters.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($wechatMode -eq "image" -and (Test-Path $imageTool)) {
            $imageFile = [System.IO.Path]::ChangeExtension($reportFile, ".png")
            Log "[$([DateTime]::Now)] rendering image brief: $imageFile"
            $renderOut = & $venvPy $imageTool $reportFile --output $imageFile 2>&1
            foreach ($line in $renderOut) { Log "    [image] $line" }
            if ($LASTEXITCODE -eq 0 -and (Test-Path $imageFile)) {
                $caption = "Market brief $date $hour:00 $sessionTag"
                Log "[$([DateTime]::Now)] pushing image brief to WeChat..."
                $pushOut = & $venvPy $pushTool --image $imageFile --caption $caption 2>&1
                foreach ($line in $pushOut) { Log "    [push] $line" }
                if ($LASTEXITCODE -eq 0) {
                    $wxPushOk = $true
                    Log "[$([DateTime]::Now)] WeChat image push OK"
                } else {
                    Log "[WARN] image push exited $LASTEXITCODE -- falling back to text speed-read"
                }
            } else {
                Log "[WARN] image render exited $LASTEXITCODE -- falling back to text speed-read"
            }
        }

        if (-not $wxPushOk) {
            # Push only the phone speed-read section as one iLink message. The
            # full report remains on disk for PC reading and listener follow-up
            # queries. Section needle built from UTF-8 bytes to keep this .ps1
            # pure ASCII.
            $secPhone = [System.Text.Encoding]::UTF8.GetString([byte[]](0xF0,0x9F,0x93,0xB1)) # 📱
            Log "[$([DateTime]::Now)] pushing $secPhone speed-read section to WeChat (1 message)..."
            $pushOut = & $venvPy $pushTool $reportFile --section $secPhone --bare 2>&1
            foreach ($line in $pushOut) { Log "    [push] $line" }
            if ($LASTEXITCODE -eq 0) {
                $wxPushOk = $true
                Log "[$([DateTime]::Now)] WeChat text push OK"
            } else {
                Log "[WARN] text push exited $LASTEXITCODE -- will fall back to email"
            }
        }
    } catch {
        Log "[WARN] WeChat push threw: $($_.Exception.Message) -- will fall back to email"
    } finally {
        $ErrorActionPreference = $prevEAP
    }
} else {
    Log "[WARN] WeChat push skipped (missing venv/pushTool) -- will fall back to email"
}

# --- 6. Email fallback (only if WeChat push failed) ---------------------
if ($wxPushOk) {
    Log "[$([DateTime]::Now)] ==== market-brief run done (WeChat only) ===="
    exit 0
}

if ($SkipEmail) {
    Log "[$([DateTime]::Now)] WeChat push failed, but -SkipEmail set; not sending fallback email."
    Log "[$([DateTime]::Now)] ==== market-brief run done (push failed, no email) ===="
    exit 0
}

Log "[$([DateTime]::Now)] sending fallback email to $($secrets.toAddress)..."

# Subject is constructed in-memory from ASCII pieces + UTF-8 bytes so the
# .ps1 file itself stays pure ASCII and never trips PowerShell's parser.
# Session-tag the Chinese label so the inbox shows 盘前 / 盘中 / 盘后 directly.
$prefixBytes = [byte[]](0xE7,0xBE,0x8E,0xE8,0x82,0xA1,0xE7,0x9B,0x98)            # 美股盘
$suffixBytes = [byte[]](0xE6,0x83,0x85,0xE6,0x8A,0xA5)                            # 情报
switch ($sessionTag) {
    'pre-market'  { $midBytes = [byte[]](0xE5,0x89,0x8D) }   # 前
    'market'      { $midBytes = [byte[]](0xE4,0xB8,0xAD) }   # 中
    'after-hours' { $midBytes = [byte[]](0xE5,0x90,0x8E) }   # 后
    default       { $midBytes = [byte[]](0xE5,0x89,0x8D) }
}
$tagText   = [System.Text.Encoding]::UTF8.GetString($prefixBytes + $midBytes + $suffixBytes)
$subject   = "[Brief $date $hour:00 $sessionTag fallback] $tagText"
$body      = Get-Content -Raw -Encoding UTF8 $reportFile

$securePass = ConvertTo-SecureString $secrets.smtpPassword -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential ($secrets.smtpUser, $securePass)

try {
    Send-MailMessage `
        -From       $secrets.fromAddress `
        -To         $secrets.toAddress `
        -Subject    $subject `
        -Body       $body `
        -SmtpServer $secrets.smtpServer `
        -Port       $secrets.smtpPort `
        -UseSsl `
        -Credential $cred `
        -Encoding   ([System.Text.Encoding]::UTF8) `
        -WarningAction SilentlyContinue
    Log "[$([DateTime]::Now)] fallback email sent to $($secrets.toAddress)"
} catch {
    Log "[ERROR] fallback email also failed: $($_.Exception.Message)"
    exit 4
}

Log "[$([DateTime]::Now)] ==== market-brief run done (email fallback) ===="
