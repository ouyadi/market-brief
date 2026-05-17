# run-listener.ps1 -- start listen_weixin.py with the right env scrubbed in.
# Same shape as run.ps1's auth/env section, just for the long-running listener.

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
# Allow power users to point at a non-default venv via $env:HERMES_VENV.
$venvPy   = if ($env:HERMES_VENV) { Join-Path $env:HERMES_VENV 'Scripts\python.exe' }
            else { Join-Path $env:USERPROFILE 'hermes-agent\.venv\Scripts\python.exe' }
$listener = Join-Path $here 'listen_weixin.py'
$secrets  = Join-Path $here 'secrets.json'

if (-not (Test-Path $venvPy))   { Write-Error "venv python missing at $venvPy"; exit 2 }
if (-not (Test-Path $listener)) { Write-Error "listen_weixin.py missing at $listener"; exit 2 }
if (-not (Test-Path $secrets))  { Write-Error "secrets.json missing at $secrets"; exit 2 }

# Strip Claude Desktop-injected env vars that would break the npm CLI 405-style.
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

$cfg = Get-Content -Raw $secrets | ConvertFrom-Json
if (-not $cfg.claudeCodeOauthToken) {
    Write-Error "secrets.json missing claudeCodeOauthToken -- run 'claude setup-token'"
    exit 2
}
$env:CLAUDE_CODE_OAUTH_TOKEN = $cfg.claudeCodeOauthToken
$env:PYTHONUTF8 = "1"
$env:LISTEN_LOG_DIR = Join-Path $here 'logs'

# Hand off
& $venvPy $listener
exit $LASTEXITCODE
