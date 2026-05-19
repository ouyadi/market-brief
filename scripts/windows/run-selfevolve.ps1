# run-selfevolve.ps1 -- start Phase 2a self-evolve scheduler with env scrubbed.
# Defaults to Codex/GPT; set MARKET_BRIEF_LLM_BACKEND=claude to use Claude CLI.

param(
    [string] $Command = "auto",
    [switch] $Force,
    [switch] $DryRun,
    [switch] $NoPush,
    [switch] $PushAlways
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$here      = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend   = if ($env:MARKET_BRIEF_LLM_BACKEND) { $env:MARKET_BRIEF_LLM_BACKEND.ToLowerInvariant() } else { "codex" }
$venvPy    = if ($env:HERMES_VENV) { Join-Path $env:HERMES_VENV 'Scripts\python.exe' }
             else { Join-Path $env:USERPROFILE 'hermes-agent\.venv\Scripts\python.exe' }
$scheduler = Join-Path $here 'selfevolve_scheduler.py'
$secrets   = Join-Path $here 'secrets.json'

if (-not (Test-Path $venvPy))    { Write-Error "venv python missing at $venvPy"; exit 2 }
if (-not (Test-Path $scheduler)) { Write-Error "selfevolve_scheduler.py missing at $scheduler"; exit 2 }

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

if ($backend -eq "claude") {
    if (-not (Test-Path $secrets))   { Write-Error "secrets.json missing at $secrets"; exit 2 }
    $cfg = Get-Content -Raw $secrets | ConvertFrom-Json
    if (-not $cfg.claudeCodeOauthToken) {
        Write-Error "secrets.json missing claudeCodeOauthToken -- run 'claude setup-token'"
        exit 2
    }
    $env:CLAUDE_CODE_OAUTH_TOKEN = $cfg.claudeCodeOauthToken
} else {
    Remove-Item "Env:CLAUDE_CODE_OAUTH_TOKEN" -ErrorAction SilentlyContinue
}

$env:MARKET_BRIEF_LLM_BACKEND = $backend
$env:PYTHONUTF8 = "1"
$env:SELFEVOLVE_LOG_DIR = Join-Path $here 'logs'
$env:MARKET_BRIEF_DIR = $here

$argsList = @($scheduler, "--command", $Command)
if ($Force)      { $argsList += "--force" }
if ($DryRun)     { $argsList += "--dry-run" }
if ($NoPush)     { $argsList += "--no-push" }
if ($PushAlways) { $argsList += "--push-always" }

& $venvPy @argsList
exit $LASTEXITCODE
