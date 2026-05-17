# quickstart.ps1 -- market-brief one-command installer / orchestrator (Windows).
#
# Run after cloning the repo:
#
#   git clone https://github.com/ouyadi/market-brief.git $env:USERPROFILE\market-brief
#   cd $env:USERPROFILE\market-brief
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\quickstart.ps1
#
# Phases are idempotent -- re-run to resume where you left off. Each phase
# prints what it's about to do and pauses if it needs user input (QR scan,
# secrets fill, group customization). Skip phases by passing -SkipPhase N.
#
# Required separately (because they live in another repo with private creds):
#   - ouyadi/mcp-chat-skills chat-mcp-setup skill (chatlog + discord-selfbot MCPs)
#     must already be installed before this script can fully succeed.

[CmdletBinding()]
param(
    [int[]] $SkipPhase = @(),
    [switch] $YesAll,           # no pauses; just plow ahead (smoke tests still skipped where they need manual scan)
    [switch] $DryRun             # print what each phase would do, don't change anything
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# quickstart.ps1 lives at scripts/windows/quickstart.ps1, so the repo root
# is two levels up. Source files are organized into three subdirs:
#   scripts/windows/  -- PowerShell + VBS launchers
#   mcp/              -- Python MCP servers + helpers (cross-platform)
#   config/           -- prompt + secrets templates
$WIN_SCRIPTS = Split-Path -Parent $MyInvocation.MyCommand.Path
$REPO_DIR    = Split-Path -Parent (Split-Path -Parent $WIN_SCRIPTS)
$MCP_DIR     = Join-Path $REPO_DIR 'mcp'
$CONFIG_DIR  = Join-Path $REPO_DIR 'config'
$SCRIPTS_DIR = Join-Path $env:USERPROFILE 'Scripts\market-brief'
$HERMES_DIR  = Join-Path $env:USERPROFILE 'hermes-agent'
$VENV_PY     = Join-Path $HERMES_DIR '.venv\Scripts\python.exe'
$TWITTER_DIR    = Join-Path $env:USERPROFILE 'twitter-mcp'
$STOCK_DIR      = Join-Path $env:USERPROFILE 'stock-mcp'
$POLYMARKET_DIR = Join-Path $env:USERPROFILE 'polymarket-mcp'
$FINJUICE_DIR   = Join-Path $env:USERPROFILE 'financialjuice-mcp'
$HERMES_HOME    = Join-Path $env:USERPROFILE '.hermes'

function Info($msg)  { Write-Host "[INFO]  $msg" -ForegroundColor Cyan }
function Ok($msg)    { Write-Host "[OK]    $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "[FAIL]  $msg" -ForegroundColor Red }
function Phase($n, $title) { Write-Host ""; Write-Host "════════════════════════════════════════════════════════════════════" -ForegroundColor Magenta; Write-Host "  Phase ${n}: $title" -ForegroundColor Magenta; Write-Host "════════════════════════════════════════════════════════════════════" -ForegroundColor Magenta }
function Pause-IfInteractive($msg) {
    if ($YesAll) { return }
    Write-Host ""
    Write-Host "  >>> $msg" -ForegroundColor Yellow
    Read-Host "      Press Enter to continue (Ctrl+C to abort)"
}
function Run($block, $name) {
    if ($DryRun) { Info "[dry-run] would: $name"; return }
    & $block
}

# ────────────────────────────────────────────────────────────────────────────
# Phase 1: prerequisite checks
# ────────────────────────────────────────────────────────────────────────────
function Phase1-Prereqs {
    Phase 1 "Prerequisite checks"
    $bad = 0

    # Python 3.11 system install
    $sysPy = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    if (Test-Path $sysPy) { Ok "system Python 3.11 at $sysPy" }
    else {
        Fail "system Python 3.11 missing at $sysPy"
        Info "  fix:  winget install --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements"
        $bad++
    }

    # Google Chrome (twitter MCP needs it)
    $chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
    if (Test-Path $chrome) { Ok "Chrome at $chrome (needed for twitter MCP)" }
    else {
        Warn "Chrome not at $chrome — Twitter MCP needs it (Playwright bundled Chromium fails under task spawn)"
        Info "  fix:  winget install Google.Chrome"
    }

    # claude CLI
    $claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
    if ($claudeCmd) { Ok "claude CLI at $($claudeCmd.Source)" }
    else {
        Fail "claude CLI missing"
        Info "  fix:  npm install -g @anthropic-ai/claude-code   (need Node.js LTS first)"
        $bad++
    }

    # chatlog daemon (assumes chat-mcp-setup skill done)
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:5030/health' -UseBasicParsing -TimeoutSec 3
        if ($r.Content -match 'ok') { Ok "chatlog daemon up on :5030" }
    } catch {
        Warn "chatlog daemon NOT responding on 127.0.0.1:5030"
        Info "  fix:  run chat-mcp-setup skill from ouyadi/mcp-chat-skills"
        Info "        (this script doesn't install chatlog; chatlog needs WeChat-specific binary)"
    }

    # discord-selfbot
    try {
        $null = Invoke-WebRequest -Uri 'http://127.0.0.1:6280/mcp' -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        Ok "discord-selfbot daemon up on :6280"
    } catch {
        if ($_.Exception.Response.StatusCode -in @(200, 405, 406)) {
            Ok "discord-selfbot daemon up on :6280 (MCP endpoint responding)"
        } else {
            Warn "discord-selfbot daemon NOT responding on 127.0.0.1:6280"
            Info "  fix:  run chat-mcp-setup skill from ouyadi/mcp-chat-skills"
        }
    }

    if ($bad -gt 0) {
        Fail "$bad blocking prereq(s) missing -- fix above and re-run."
        if (-not $YesAll) { exit 2 }
    } else {
        Ok "all critical prereqs present"
    }
}

# ────────────────────────────────────────────────────────────────────────────
# Phase 2: Python venv + all MCP server deps
# ────────────────────────────────────────────────────────────────────────────
function Phase2-PythonEnv {
    Phase 2 "Python venv + MCP server deps"

    if (-not (Test-Path $VENV_PY)) {
        Info "creating hermes-agent venv at $HERMES_DIR"
        $sysPy = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
        Run { New-Item -ItemType Directory -Force -Path $HERMES_DIR | Out-Null; & $sysPy -m venv "$HERMES_DIR\.venv" } "python -m venv"
    } else {
        Ok "venv exists at $HERMES_DIR\.venv"
    }

    Info "installing/upgrading Python deps (this can take 3-5 minutes first time)..."
    Run { & $VENV_PY -m pip install --quiet --upgrade pip } "pip upgrade"
    Run {
        & $VENV_PY -m pip install --quiet `
            hermes-agent qrcode Pillow aiohttp cryptography `
            mcp playwright yfinance
    } "pip install all deps"

    # Playwright chromium (needed at install time even though daemon uses
    # user-installed Chrome via channel='chrome')
    Info "installing Playwright Chromium browser (~150MB)..."
    Run { & $VENV_PY -m playwright install chromium } "playwright install"

    Ok "Python env + all MCP deps ready"
}

# ────────────────────────────────────────────────────────────────────────────
# Phase 3: copy this repo's files into the user's runtime dirs
# ────────────────────────────────────────────────────────────────────────────
function Phase3-CopyFiles {
    Phase 3 "Copy runtime files to user dirs"

    # market-brief dir
    if (-not (Test-Path $SCRIPTS_DIR)) {
        Info "creating $SCRIPTS_DIR"
        Run { New-Item -ItemType Directory -Force -Path $SCRIPTS_DIR | Out-Null } "mkdir"
    }
    # Each entry: source-dir, filename (gets copied into $SCRIPTS_DIR)
    $marketFiles = @(
        @{Dir=$WIN_SCRIPTS; Name='run.ps1'},
        @{Dir=$WIN_SCRIPTS; Name='schedule-install.ps1'},
        @{Dir=$WIN_SCRIPTS; Name='run-listener.ps1'},
        @{Dir=$WIN_SCRIPTS; Name='install-listener.ps1'},
        @{Dir=$WIN_SCRIPTS; Name='hermes-py.ps1'},
        @{Dir=$WIN_SCRIPTS; Name='run-hidden.vbs'},   # no-flash launcher used by schedule-install.ps1 + install-listener.ps1
        @{Dir=$MCP_DIR;     Name='push_weixin.py'},
        @{Dir=$MCP_DIR;     Name='qr_login_bootstrap.py'},
        @{Dir=$MCP_DIR;     Name='listen_weixin.py'},
        @{Dir=$CONFIG_DIR;  Name='secrets.example.json'}
    )
    foreach ($f in $marketFiles) {
        $src = Join-Path $f.Dir $f.Name
        $dst = Join-Path $SCRIPTS_DIR $f.Name
        if ((Test-Path $src) -and -not (Test-Path $dst)) {
            Run { Copy-Item $src $dst -Force } "cp $($f.Name) -> Scripts/market-brief"
            Ok "  copied $($f.Name)"
        } elseif (Test-Path $dst) {
            Info "  $($f.Name) exists in $SCRIPTS_DIR (keeping user's version)"
        }
    }

    # prompt.md from template if missing
    $promptDst = Join-Path $SCRIPTS_DIR 'prompt.md'
    if (-not (Test-Path $promptDst)) {
        $tpl = Join-Path $CONFIG_DIR 'prompt.template.md'
        Run { Copy-Item $tpl $promptDst -Force } "cp prompt.template.md -> prompt.md"
        Warn "  created prompt.md from template -- YOU MUST edit it to add your groups/handles/tickers"
    }

    # memory.md from template if missing (cross-run persistent user angles;
    # run.ps1 prepends it to prompt.md before each brief)
    $memDst = Join-Path $SCRIPTS_DIR 'memory.md'
    if (-not (Test-Path $memDst)) {
        $tpl = Join-Path $CONFIG_DIR 'memory.template.md'
        if (Test-Path $tpl) {
            Run { Copy-Item $tpl $memDst -Force } "cp memory.template.md -> memory.md"
            Info "  created memory.md from template -- listener will append durable user feedback here over time"
        }
    }

    # secrets.json starter
    $secretsDst = Join-Path $SCRIPTS_DIR 'secrets.json'
    if (-not (Test-Path $secretsDst)) {
        $tpl = Join-Path $SCRIPTS_DIR 'secrets.example.json'
        Run {
            Copy-Item $tpl $secretsDst -Force
            icacls $secretsDst /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
        } "create secrets.json"
        Warn "  created secrets.json -- YOU MUST fill in: claudeCodeOauthToken (run 'claude setup-token'), smtpUser/smtpPassword (Gmail App Password)"
    }

    # twitter-mcp dir
    Run { New-Item -ItemType Directory -Force -Path $TWITTER_DIR | Out-Null } "mkdir twitter-mcp"
    Run { Copy-Item (Join-Path $MCP_DIR     'twitter_playwright_mcp.py') $TWITTER_DIR -Force } "cp twitter_playwright_mcp.py"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'install-twitter-mcp.ps1')   $TWITTER_DIR -Force } "cp install-twitter-mcp.ps1"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'run-hidden.vbs')            $TWITTER_DIR -Force } "cp run-hidden.vbs"
    # twitter-mcp .env left for user to create (cookies)
    $twEnv = Join-Path $TWITTER_DIR '.env'
    if (-not (Test-Path $twEnv)) {
        Warn "  $twEnv does NOT exist -- you'll need to create it manually with X cookies (see Phase 4 prompts)"
    }

    # stock-mcp dir
    Run { New-Item -ItemType Directory -Force -Path $STOCK_DIR | Out-Null } "mkdir stock-mcp"
    Run { Copy-Item (Join-Path $MCP_DIR     'stock_price_mcp.py')     $STOCK_DIR -Force } "cp stock_price_mcp.py"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'install-stock-mcp.ps1')  $STOCK_DIR -Force } "cp install-stock-mcp.ps1"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'run-hidden.vbs')         $STOCK_DIR -Force } "cp run-hidden.vbs"

    # polymarket-mcp dir
    Run { New-Item -ItemType Directory -Force -Path $POLYMARKET_DIR | Out-Null } "mkdir polymarket-mcp"
    Run { Copy-Item (Join-Path $MCP_DIR     'polymarket_mcp.py')         $POLYMARKET_DIR -Force } "cp polymarket_mcp.py"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'install-polymarket-mcp.ps1') $POLYMARKET_DIR -Force } "cp install-polymarket-mcp.ps1"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'run-hidden.vbs')             $POLYMARKET_DIR -Force } "cp run-hidden.vbs"

    # financialjuice-mcp dir
    Run { New-Item -ItemType Directory -Force -Path $FINJUICE_DIR | Out-Null } "mkdir financialjuice-mcp"
    Run { Copy-Item (Join-Path $MCP_DIR     'financialjuice_mcp.py')         $FINJUICE_DIR -Force } "cp financialjuice_mcp.py"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'install-financialjuice-mcp.ps1') $FINJUICE_DIR -Force } "cp install-financialjuice-mcp.ps1"
    Run { Copy-Item (Join-Path $WIN_SCRIPTS 'run-hidden.vbs')                 $FINJUICE_DIR -Force } "cp run-hidden.vbs"

    Ok "all runtime files placed"
}

# ────────────────────────────────────────────────────────────────────────────
# Phase 4: user-interactive steps (cannot be auto'd)
# ────────────────────────────────────────────────────────────────────────────
function Phase4-Interactive {
    Phase 4 "User-interactive setup (cannot be automated)"

    Write-Host ""
    Write-Host "The following MUST happen manually -- this script will pause after each:" -ForegroundColor Yellow
    Write-Host "  4a. Fill secrets.json (Claude OAuth + Gmail App Password)" -ForegroundColor Yellow
    Write-Host "  4b. Customize prompt.md (your Discord channels + WeChat groups)" -ForegroundColor Yellow
    Write-Host "  4c. Bind WeChat via QR scan (8-min window, your phone)" -ForegroundColor Yellow
    Write-Host "  4d. Extract X cookies → twitter-mcp/.env (DevTools, 2 min)" -ForegroundColor Yellow

    # 4a secrets.json
    $secretsPath = Join-Path $SCRIPTS_DIR 'secrets.json'
    if ((Get-Content -Raw $secretsPath) -match 'sk-ant-oat01-\.\.\.|you@gmail\.com|xxxx xxxx xxxx xxxx') {
        Pause-IfInteractive "Opening secrets.json in notepad now -- fill in the placeholders and save."
        Run { notepad $secretsPath } "notepad secrets.json"
        Pause-IfInteractive "Done editing secrets.json?"
    } else {
        Ok "  4a. secrets.json looks filled (no obvious placeholders)"
    }

    # 4b prompt.md
    $promptPath = Join-Path $SCRIPTS_DIR 'prompt.md'
    if ((Get-Content -Raw $promptPath) -match '<\s*服务器名\s*>|<\s*群名\s*>|<TICKER>|<handle>') {
        Pause-IfInteractive "Opening prompt.md -- replace placeholders in the Discord/WeChat/大V/watchlist tables."
        Run { notepad $promptPath } "notepad prompt.md"
        Pause-IfInteractive "Done editing prompt.md?"
    } else {
        Ok "  4b. prompt.md looks customized (no obvious placeholders)"
    }

    # 4c iLink QR (only if .hermes/.env doesn't exist or lacks WEIXIN_TOKEN)
    $envFile = Join-Path $HERMES_HOME '.env'
    $needQr = $true
    if (Test-Path $envFile) {
        $envText = Get-Content -Raw $envFile
        if ($envText -match 'WEIXIN_TOKEN="[^"]{20,}"') { $needQr = $false }
    }
    if ($needQr) {
        Pause-IfInteractive "Will now run qr_login_bootstrap.py -- a QR code PNG will pop up; scan with your phone's WeChat within 8 min."
        $qrScript = Join-Path $SCRIPTS_DIR 'qr_login_bootstrap.py'
        Run { & $VENV_PY $qrScript } "qr_login_bootstrap.py"
        Pause-IfInteractive "QR scan done? .hermes/.env should now have WEIXIN_TOKEN."
    } else {
        Ok "  4c. ~/.hermes/.env already has WEIXIN_TOKEN -- skipping QR step"
    }

    # 4d X cookies
    $twEnv = Join-Path $TWITTER_DIR '.env'
    if (-not (Test-Path $twEnv)) {
        Write-Host ""
        Write-Host "  4d. X cookies needed. Open Chrome -> https://x.com (login) -> F12 -> Application -> Cookies -> https://x.com" -ForegroundColor Yellow
        Write-Host "      Copy these 3 cookies' VALUE: auth_token, ct0, twid" -ForegroundColor Yellow
        Write-Host "      Write a file at $twEnv with:" -ForegroundColor Yellow
        Write-Host '        AUTH_METHOD=cookies' -ForegroundColor Gray
        Write-Host '        TWITTER_COOKIES=["auth_token=...; Domain=.x.com","ct0=...; Domain=.x.com","twid=...; Domain=.x.com"]' -ForegroundColor Gray
        Write-Host '        # PORT/TWITTER_MCP_PORT optional; defaults to 3031' -ForegroundColor Gray
        Pause-IfInteractive "Press Enter once $twEnv is saved (or skip Twitter MCP entirely if you don't want X integration)"
    } else {
        Ok "  4d. twitter-mcp/.env exists -- skipping cookie prompt"
    }
}

# ────────────────────────────────────────────────────────────────────────────
# Phase 5: register all scheduled tasks
# ────────────────────────────────────────────────────────────────────────────
function Phase5-Tasks {
    Phase 5 "Register scheduled tasks"

    # MarketBrief
    Info "registering MarketBrief task (hourly 08:00-22:00)"
    Run { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $SCRIPTS_DIR 'schedule-install.ps1') | Out-Null } "schedule-install.ps1"
    Ok "MarketBrief registered"

    # WeixinListener
    Info "registering WeixinListener task (At log on)"
    Run { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $SCRIPTS_DIR 'install-listener.ps1') | Out-Null } "install-listener.ps1"
    Ok "WeixinListener registered"

    # TwitterMCP (skip if no .env)
    $twEnv = Join-Path $TWITTER_DIR '.env'
    if (Test-Path $twEnv) {
        Info "registering TwitterMCP task (At log on)"
        Run { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $TWITTER_DIR 'install-twitter-mcp.ps1') | Out-Null } "install-twitter-mcp.ps1"
        Ok "TwitterMCP registered"
    } else {
        Warn "skipping TwitterMCP (twitter-mcp/.env missing)"
    }

    # StockPriceMCP
    Info "registering StockPriceMCP task (At log on)"
    Run { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $STOCK_DIR 'install-stock-mcp.ps1') | Out-Null } "install-stock-mcp.ps1"
    Ok "StockPriceMCP registered"

    # PolymarketMCP
    Info "registering PolymarketMCP task (At log on)"
    Run { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $POLYMARKET_DIR 'install-polymarket-mcp.ps1') | Out-Null } "install-polymarket-mcp.ps1"
    Ok "PolymarketMCP registered"

    # FinancialJuiceMCP
    Info "registering FinancialJuiceMCP task (At log on)"
    Run { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $FINJUICE_DIR 'install-financialjuice-mcp.ps1') | Out-Null } "install-financialjuice-mcp.ps1"
    Ok "FinancialJuiceMCP registered"
}

# ────────────────────────────────────────────────────────────────────────────
# Phase 6: start daemons + register MCPs with claude
# ────────────────────────────────────────────────────────────────────────────
function Phase6-Activate {
    Phase 6 "Start daemons + register claude MCPs"

    foreach ($t in @('WeixinListener', 'TwitterMCP', 'StockPriceMCP', 'PolymarketMCP', 'FinancialJuiceMCP')) {
        $exists = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($exists) {
            Info "starting $t..."
            Run { Start-ScheduledTask -TaskName $t } "Start $t"
        }
    }
    Start-Sleep -Seconds 6

    # Register MCPs (idempotent: remove+add)
    Info "registering MCPs with claude..."
    foreach ($pair in @(
        @{ name = 'twitter';        url = 'http://127.0.0.1:3031/mcp' },
        @{ name = 'stock-price';    url = 'http://127.0.0.1:3032/mcp' },
        @{ name = 'polymarket';     url = 'http://127.0.0.1:3033/mcp' },
        @{ name = 'financialjuice'; url = 'http://127.0.0.1:3034/mcp' }
    )) {
        # only if port is listening
        $port = ($pair.url -replace '.*:(\d+)/mcp','$1')
        $listening = (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count
        if ($listening -gt 0) {
            Run { & claude mcp remove --scope user $pair.name 2>$null } "remove old $($pair.name)"
            Run { & claude mcp add --transport http --scope user $pair.name $pair.url | Out-Null } "add $($pair.name)"
            Ok "  $($pair.name) -> $($pair.url)"
        } else {
            Warn "  skipping $($pair.name) -- nothing listening on $port"
        }
    }

    Info "current MCP list:"
    & claude mcp list 2>&1 | Select-String 'twitter|chatlog|discord|stock'
}

# ────────────────────────────────────────────────────────────────────────────
# Phase 7: smoke test
# ────────────────────────────────────────────────────────────────────────────
function Phase7-Smoke {
    Phase 7 "Smoke test"

    Info "triggering one MarketBrief now (this takes 5-10 min; uses iLink quota + email)"
    Pause-IfInteractive "Confirm to fire? Brief will land in C:\Users\<u>\Reports\YYYY-MM-DD-HH-brief.md"
    Run { & (Join-Path $SCRIPTS_DIR 'run.ps1') -SkipEmail } "run.ps1 -SkipEmail"
    Ok "smoke test done -- check WeChat for the ⚡ section push"
}

# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "market-brief quickstart installer" -ForegroundColor Magenta
Write-Host "Repo:   $REPO_DIR" -ForegroundColor Gray
Write-Host "Target user runtime dirs:" -ForegroundColor Gray
Write-Host "  Scripts:      $SCRIPTS_DIR" -ForegroundColor Gray
Write-Host "  Hermes venv:  $HERMES_DIR" -ForegroundColor Gray
Write-Host "  Twitter MCP:  $TWITTER_DIR" -ForegroundColor Gray
Write-Host "  Stock MCP:    $STOCK_DIR" -ForegroundColor Gray
Write-Host "  iLink home:   $HERMES_HOME" -ForegroundColor Gray
if ($DryRun) { Warn "DRY RUN mode -- no changes will be made" }
if ($YesAll) { Warn "Yes-all mode -- skipping interactive pauses (will not be able to manually fill secrets/scan QR)" }

$phases = @{
    1 = { Phase1-Prereqs }
    2 = { Phase2-PythonEnv }
    3 = { Phase3-CopyFiles }
    4 = { Phase4-Interactive }
    5 = { Phase5-Tasks }
    6 = { Phase6-Activate }
    7 = { Phase7-Smoke }
}

foreach ($n in 1..7) {
    if ($n -in $SkipPhase) {
        Warn "Phase ${n}: SKIPPED (--SkipPhase $n)"
        continue
    }
    & $phases[$n]
}

Write-Host ""
Ok "Quickstart complete. Read SKILL.md / README.md for operational details + slash commands."
Write-Host ""
