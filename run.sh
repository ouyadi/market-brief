#!/usr/bin/env bash
# run.sh -- macOS / Linux equivalent of run.ps1.
#
# Pipeline:
#   1) run Claude Code with prompt.md
#   2) read produced report
#   3) push to WeChat via Hermes iLink   (primary channel)
#   4) if WeChat push failed, send email (fallback)
#
# Env vars (override defaults):
#   MARKET_BRIEF_DIR    runtime dir              (default: ~/Scripts/market-brief)
#   REPORTS_DIR         where reports land       (default: ~/Reports)
#   HERMES_VENV         venv with Hermes Agent   (default: ~/hermes-agent/.venv)
#   SKIP_EMAIL=1        never send fallback email
#
# Time-zone: EDT hardcoded (TZ=America/New_York used for date math).
# Same scheduling envelope as Windows: hourly 08-22 EDT.

set -euo pipefail

MARKET_BRIEF_DIR="${MARKET_BRIEF_DIR:-$HOME/Scripts/market-brief}"
REPORTS_DIR="${REPORTS_DIR:-$HOME/Reports}"
HERMES_VENV="${HERMES_VENV:-$HOME/hermes-agent/.venv}"

PROMPT_FILE="$MARKET_BRIEF_DIR/prompt.md"
SECRETS_FILE="$MARKET_BRIEF_DIR/secrets.json"
LOG_DIR="$MARKET_BRIEF_DIR/logs"
mkdir -p "$LOG_DIR" "$REPORTS_DIR"

# --- EDT-aware hour (assumes TZ=America/New_York) ---
# Add 30s slop so a launchd fire that lands at HH:59:58 (a second or two
# early due to drift) still resolves to the intended next-hour bucket.
# Mirrors run.ps1's .AddSeconds(30) behavior.
DATE="$(TZ=America/New_York date -v+30S +%Y-%m-%d 2>/dev/null || TZ=America/New_York date -d '+30 seconds' +%Y-%m-%d)"
HOUR="$(TZ=America/New_York date -v+30S +%H 2>/dev/null || TZ=America/New_York date -d '+30 seconds' +%H)"
REPORT_FILE="$REPORTS_DIR/${DATE}-${HOUR}-brief.md"
LOG_FILE="$LOG_DIR/${DATE}.log"

log() {
    local line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$line"
    echo "$line" >> "$LOG_FILE"
}

# --- Stamp into env for Claude (matches Windows run.ps1) ---
export MARKET_BRIEF_OUTPUT="$REPORT_FILE"

# session label
hour_int=$((10#$HOUR))
if   [ "$hour_int" -lt 9  ]; then SESSION="pre-market"
elif [ "$hour_int" -le 16 ]; then SESSION="market"
else                              SESSION="after-hours"
fi

log "==== market-brief run start (hour=$HOUR, session=$SESSION) ===="

# Strip Claude Desktop-injected env (same list as run.ps1)
for v in ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_BASE_URL \
         ANTHROPIC_BEDROCK ANTHROPIC_VERTEX_PROJECT_ID ANTHROPIC_VERTEX_REGION \
         CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH \
         CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION_ID CLAUDE_CODE_DISABLE_CRON \
         CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL \
         CLAUDE_AGENT_SDK_VERSION CLAUDECODE; do
    unset "$v"
done

# Sanity
[ -f "$SECRETS_FILE" ] || { log "[ERROR] secrets.json missing: $SECRETS_FILE"; exit 2; }

# Load Claude OAuth token from secrets.json (needs jq or python)
if command -v jq >/dev/null; then
    OAUTH="$(jq -r .claudeCodeOauthToken "$SECRETS_FILE")"
else
    OAUTH="$($HERMES_VENV/bin/python -c "import json; print(json.load(open('$SECRETS_FILE'))['claudeCodeOauthToken'])")"
fi
[ -n "$OAUTH" ] && [ "$OAUTH" != "null" ] || { log "[ERROR] claudeCodeOauthToken missing"; exit 2; }
export CLAUDE_CODE_OAUTH_TOKEN="$OAUTH"

# Run Claude
log "launching claude --print (takes a few minutes)..."
claude --print --dangerously-skip-permissions --output-format text < "$PROMPT_FILE" \
    2>&1 | while IFS= read -r line; do log "    [claude] $line"; done

[ -f "$REPORT_FILE" ] || { log "[ERROR] report file missing: $REPORT_FILE"; exit 3; }
log "report ready: $REPORT_FILE"

# --- Push WeChat (primary) ---
WX_OK=0
PUSH_TOOL="$MARKET_BRIEF_DIR/push_weixin.py"
VENV_PY="$HERMES_VENV/bin/python"

if [ -x "$VENV_PY" ] && [ -f "$PUSH_TOOL" ]; then
    log "pushing '⚡' section to WeChat..."
    if "$VENV_PY" "$PUSH_TOOL" "$REPORT_FILE" --section "⚡" 2>&1 | while IFS= read -r line; do log "    [push] $line"; done; then
        WX_OK=1
        log "WeChat push OK"
    else
        log "[WARN] WeChat push failed -- will fall back to email"
    fi
else
    log "[WARN] push tooling missing -- skipping to email fallback"
fi

if [ "$WX_OK" -eq 1 ]; then
    log "==== run done (WeChat only) ===="
    exit 0
fi

# --- Email fallback ---
if [ "${SKIP_EMAIL:-0}" = "1" ]; then
    log "SKIP_EMAIL=1 -- not sending fallback email"
    log "==== run done (push failed, no email) ===="
    exit 0
fi

log "sending fallback email..."
# Use python helper for SMTP (cross-platform vs needing mailx / postfix)
"$VENV_PY" - "$REPORT_FILE" "$SECRETS_FILE" "$DATE" "$HOUR" "$SESSION" <<'PYEOF'
import json, smtplib, ssl, sys
from email.message import EmailMessage
from pathlib import Path

report_path, secrets_path, date_s, hour_s, session = sys.argv[1:6]
secrets = json.loads(Path(secrets_path).read_text(encoding="utf-8"))
body = Path(report_path).read_text(encoding="utf-8")

# Session-tag the subject so visually scanning the inbox tells you whether
# the report is pre-market, intraday, or after-hours.
session_label = {
    "pre-market":  "美股盘前情报",
    "market":      "美股盘中情报",
    "after-hours": "美股盘后情报",
}.get(session, "美股情报")

msg = EmailMessage()
msg["Subject"] = f"[Brief {date_s} {hour_s}:00 {session} fallback] {session_label}"
msg["From"]    = secrets["fromAddress"]
msg["To"]      = secrets["toAddress"]
msg.set_content(body, charset="utf-8")

with smtplib.SMTP(secrets["smtpServer"], int(secrets["smtpPort"])) as s:
    s.starttls(context=ssl.create_default_context())
    s.login(secrets["smtpUser"], secrets["smtpPassword"])
    s.send_message(msg)
print(f"sent to {secrets['toAddress']}")
PYEOF

log "fallback email sent"
log "==== run done (email fallback) ===="
