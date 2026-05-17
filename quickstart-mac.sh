#!/usr/bin/env bash
# quickstart-mac.sh -- macOS one-command installer for market-brief.
#
# Run after cloning:
#   git clone https://github.com/ouyadi/market-brief.git ~/market-brief
#   cd ~/market-brief
#   bash quickstart-mac.sh
#
# Mirrors quickstart.ps1 (Windows) one-to-one:
#   1. Prereqs       (Python 3.11, Chrome, claude CLI, chatlog/discord daemons)
#   2. Python venv   (~/hermes-agent/.venv) + pip install all deps + playwright chromium
#   3. Copy files    to ~/Scripts/market-brief, ~/twitter-mcp, ~/stock-mcp
#   4. Interactive   secrets.json / prompt.md / QR scan / X cookies
#   5. launchd       load 4 plists from ~/Library/LaunchAgents/
#   6. Activate      claude mcp add (HTTP MCPs)
#   7. Smoke test    one ./run.sh in pre-market-only mode
#
# Re-runnable: each phase auto-skips if already done.
# Skip specific phases with: SKIP_PHASES="1 7" bash quickstart-mac.sh
#
# Caveat: chatlog (WeChat 4.x reader) is the only piece this script CANNOT
# install -- it's in the separate ouyadi/mcp-chat-skills repo (chat-mcp-setup
# skill, host/macos path). Phase 1 only checks that chatlog daemon is up.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$HOME/Scripts/market-brief"
HERMES_DIR="$HOME/hermes-agent"
VENV_PY="$HERMES_DIR/.venv/bin/python"
TWITTER_DIR="$HOME/twitter-mcp"
STOCK_DIR="$HOME/stock-mcp"
HERMES_HOME="$HOME/.hermes"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

SKIP_PHASES="${SKIP_PHASES:-}"

C_RESET='\033[0m'
C_RED='\033[31m'
C_GREEN='\033[32m'
C_YELLOW='\033[33m'
C_CYAN='\033[36m'
C_MAGENTA='\033[35m'

info()  { printf "${C_CYAN}[INFO]${C_RESET}  %s\n" "$*"; }
ok()    { printf "${C_GREEN}[OK]${C_RESET}    %s\n" "$*"; }
warn()  { printf "${C_YELLOW}[WARN]${C_RESET}  %s\n" "$*"; }
fail()  { printf "${C_RED}[FAIL]${C_RESET}  %s\n" "$*"; }
phase() {
    echo
    printf "${C_MAGENTA}════════════════════════════════════════════════════════════════════${C_RESET}\n"
    printf "${C_MAGENTA}  Phase %s: %s${C_RESET}\n" "$1" "$2"
    printf "${C_MAGENTA}════════════════════════════════════════════════════════════════════${C_RESET}\n"
}
pause_if() {
    [ "${YES_ALL:-0}" = "1" ] && return
    echo
    printf "${C_YELLOW}  >>> %s${C_RESET}\n" "$1"
    printf "      Press Enter to continue (Ctrl+C to abort): "
    read -r _
}
should_skip() {
    for p in $SKIP_PHASES; do [ "$p" = "$1" ] && return 0; done
    return 1
}

# ────────────────────────────────────────────────────────────────────────────
phase1_prereqs() {
    phase 1 "Prerequisite checks"
    local bad=0

    # Python 3.11
    if command -v python3.11 >/dev/null; then
        ok "python3.11 at $(command -v python3.11) ($(python3.11 --version))"
    else
        fail "python3.11 not in PATH"
        info "  fix:  brew install python@3.11"
        bad=$((bad+1))
    fi

    # Google Chrome (twitter MCP needs it)
    if [ -d "/Applications/Google Chrome.app" ]; then
        ok "Chrome at /Applications/Google Chrome.app (twitter MCP needs it)"
    else
        warn "Chrome not in /Applications -- twitter MCP needs it"
        info "  fix:  brew install --cask google-chrome"
    fi

    # claude CLI
    if command -v claude >/dev/null; then
        ok "claude CLI at $(command -v claude)"
    else
        fail "claude CLI missing"
        info "  fix:  npm install -g @anthropic-ai/claude-code"
        bad=$((bad+1))
    fi

    # chatlog daemon (from chat-mcp-setup skill -- separate install)
    if curl -fsS --max-time 3 http://127.0.0.1:5030/health 2>/dev/null | grep -q ok; then
        ok "chatlog daemon on :5030"
    else
        warn "chatlog daemon NOT responding on 127.0.0.1:5030"
        info "  fix:  install ouyadi/mcp-chat-skills chat-mcp-setup skill (host/macos path)"
        info "  this script doesn't install chatlog because chatlog ↔ WeChat 4.x mac"
        info "  is itself unverified at the time of writing."
    fi

    # discord-selfbot
    if curl -fsS --max-time 3 -o /dev/null http://127.0.0.1:6280/mcp 2>/dev/null; then
        ok "discord-selfbot daemon on :6280"
    else
        # MCP endpoint may return 405/406 for unauthenticated GET; check connect-able
        if (echo >/dev/tcp/127.0.0.1/6280) 2>/dev/null; then
            ok "discord-selfbot daemon on :6280 (TCP up)"
        else
            warn "discord-selfbot daemon NOT responding on :6280"
            info "  fix:  same chat-mcp-setup skill"
        fi
    fi

    if [ "$bad" -gt 0 ]; then
        fail "$bad blocking prereq(s) -- fix above and re-run."
        [ "${YES_ALL:-0}" = "1" ] || exit 2
    else
        ok "all critical prereqs present"
    fi
}

# ────────────────────────────────────────────────────────────────────────────
phase2_pyenv() {
    phase 2 "Python venv + MCP server deps"

    if [ ! -x "$VENV_PY" ]; then
        info "creating hermes-agent venv at $HERMES_DIR"
        mkdir -p "$HERMES_DIR"
        python3.11 -m venv "$HERMES_DIR/.venv"
    else
        ok "venv exists at $HERMES_DIR/.venv"
    fi

    info "pip install all deps (first time: 3-5 minutes)"
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install --quiet \
        hermes-agent qrcode Pillow aiohttp cryptography \
        mcp playwright yfinance

    info "installing Playwright Chromium (~150MB)..."
    "$VENV_PY" -m playwright install chromium

    ok "Python env + all MCP deps ready"
}

# ────────────────────────────────────────────────────────────────────────────
phase3_copy() {
    phase 3 "Copy runtime files to user dirs"

    mkdir -p "$SCRIPTS_DIR" "$TWITTER_DIR" "$STOCK_DIR"

    # market-brief runtime
    for f in run.sh push_weixin.py qr_login_bootstrap.py \
             listen_weixin.py secrets.example.json; do
        if [ -f "$REPO_DIR/$f" ] && [ ! -f "$SCRIPTS_DIR/$f" ]; then
            cp "$REPO_DIR/$f" "$SCRIPTS_DIR/$f"
            ok "  copied $f"
        elif [ -f "$SCRIPTS_DIR/$f" ]; then
            info "  $f exists (keeping user version)"
        fi
    done
    [ -f "$SCRIPTS_DIR/run.sh" ] && chmod +x "$SCRIPTS_DIR/run.sh"

    # prompt.md from template
    if [ ! -f "$SCRIPTS_DIR/prompt.md" ]; then
        cp "$REPO_DIR/prompt.template.md" "$SCRIPTS_DIR/prompt.md"
        warn "  created prompt.md from template -- YOU MUST edit it"
    fi

    # secrets.json starter
    if [ ! -f "$SCRIPTS_DIR/secrets.json" ]; then
        cp "$SCRIPTS_DIR/secrets.example.json" "$SCRIPTS_DIR/secrets.json"
        chmod 600 "$SCRIPTS_DIR/secrets.json"
        warn "  created secrets.json -- YOU MUST fill claudeCodeOauthToken + Gmail App Password"
    fi

    # MCP dirs
    cp "$REPO_DIR/twitter_playwright_mcp.py"  "$TWITTER_DIR/"
    cp "$REPO_DIR/stock_price_mcp.py"         "$STOCK_DIR/"

    ok "all runtime files placed"
}

# ────────────────────────────────────────────────────────────────────────────
phase4_interactive() {
    phase 4 "User-interactive setup"

    echo "Manual steps (script pauses after each):"
    echo "  4a. Fill secrets.json (Claude OAuth + Gmail App Password)"
    echo "  4b. Customize prompt.md (Discord channels + WeChat groups + 大V + watchlist)"
    echo "  4c. Bind WeChat via iLink QR (8-min window, your phone)"
    echo "  4d. Extract X cookies -> twitter-mcp/.env"

    # 4a -- check for the literal placeholder values from secrets.example.json
    if grep -qE 'sk-ant-oat01-\.\.\.|you@gmail\.com|xxxx xxxx xxxx xxxx' \
            "$SCRIPTS_DIR/secrets.json" 2>/dev/null; then
        pause_if "Opening secrets.json in default editor — fill in then save."
        ${EDITOR:-nano} "$SCRIPTS_DIR/secrets.json"
    else
        ok "  4a. secrets.json looks filled"
    fi

    # 4b
    if grep -qE '<服务器名>|<群名>|<TICKER>|<handle>' "$SCRIPTS_DIR/prompt.md" 2>/dev/null; then
        pause_if "Opening prompt.md in default editor — replace placeholders."
        ${EDITOR:-nano} "$SCRIPTS_DIR/prompt.md"
    else
        ok "  4b. prompt.md looks customized"
    fi

    # 4c
    local need_qr=1
    [ -f "$HERMES_HOME/.env" ] && grep -qE 'WEIXIN_TOKEN="[^"]{20,}"' "$HERMES_HOME/.env" && need_qr=0
    if [ "$need_qr" = "1" ]; then
        pause_if "Will run qr_login_bootstrap.py — QR PNG opens; scan with phone WeChat."
        "$VENV_PY" "$SCRIPTS_DIR/qr_login_bootstrap.py"
    else
        ok "  4c. ~/.hermes/.env already has WEIXIN_TOKEN"
    fi

    # 4d
    if [ ! -f "$TWITTER_DIR/.env" ]; then
        cat <<EOF

  4d. X cookies needed. In Chrome:
      1. Open https://x.com (logged in)
      2. DevTools (Cmd+Opt+I) -> Application -> Cookies -> https://x.com
      3. Copy values of: auth_token, ct0, twid
      4. Save the following to $TWITTER_DIR/.env (Domain= field is decorative,
         the MCP injects cookies as .x.com regardless):
           AUTH_METHOD=cookies
           TWITTER_COOKIES=["auth_token=...; Domain=.x.com","ct0=...; Domain=.x.com","twid=...; Domain=.x.com"]
           # PORT is optional; defaults to 3031 (see twitter_playwright_mcp.py).
           # Only set TWITTER_MCP_PORT here if you need a non-default port.

EOF
        pause_if "Press Enter once $TWITTER_DIR/.env is saved (or skip Twitter MCP entirely)"
    else
        ok "  4d. twitter-mcp/.env exists"
    fi
}

# ────────────────────────────────────────────────────────────────────────────
phase5_launchd() {
    phase 5 "Load launchd agents"

    mkdir -p "$LAUNCH_AGENTS"

    # Always load: market-brief, weixin-listener, stock-mcp
    # Conditionally: twitter-mcp (only if .env exists)
    local plists=(
        com.ouyadi.market-brief.plist
        com.ouyadi.weixin-listener.plist
        com.ouyadi.stock-mcp.plist
    )
    [ -f "$TWITTER_DIR/.env" ] && plists+=(com.ouyadi.twitter-mcp.plist)

    for plist in "${plists[@]}"; do
        local src="$REPO_DIR/launchd/$plist"
        local dst="$LAUNCH_AGENTS/$plist"
        cp "$src" "$dst"
        # Unload (idempotent) then load
        launchctl unload "$dst" 2>/dev/null || true
        launchctl load "$dst"
        ok "  loaded $plist"
    done

    [ ! -f "$TWITTER_DIR/.env" ] && warn "  skipped com.ouyadi.twitter-mcp.plist (no .env)"
}

# ────────────────────────────────────────────────────────────────────────────
phase6_activate() {
    phase 6 "Register HTTP MCPs with claude"

    sleep 6  # give launchd a moment to spawn daemons

    # Try registering MCPs only if their ports are up.
    register_if_up() {
        local name="$1" port="$2" url="$3"
        if (echo >/dev/tcp/127.0.0.1/"$port") 2>/dev/null; then
            claude mcp remove --scope user "$name" 2>/dev/null || true
            claude mcp add --transport http --scope user "$name" "$url" >/dev/null
            ok "  $name -> $url"
        else
            warn "  $name skipped (nothing on :$port)"
        fi
    }

    register_if_up twitter     3031 http://127.0.0.1:3031/mcp
    register_if_up stock-price 3032 http://127.0.0.1:3032/mcp

    info "current MCP list:"
    claude mcp list 2>&1 | grep -E 'twitter|chatlog|discord|stock' || true
}

# ────────────────────────────────────────────────────────────────────────────
phase7_smoke() {
    phase 7 "Smoke test"
    pause_if "About to fire one ./run.sh -- takes 5-10 min + uses iLink quota."
    SKIP_EMAIL=1 "$SCRIPTS_DIR/run.sh" || warn "smoke test exited non-zero (check log)"
    ok "smoke test done -- check WeChat for the ⚡ push"
}

# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

echo
printf "${C_MAGENTA}market-brief quickstart installer (macOS)${C_RESET}\n"
echo "Repo:    $REPO_DIR"
echo "Targets:"
echo "  Scripts:      $SCRIPTS_DIR"
echo "  Hermes venv:  $HERMES_DIR"
echo "  Twitter MCP:  $TWITTER_DIR"
echo "  Stock MCP:    $STOCK_DIR"
echo "  iLink home:   $HERMES_HOME"
echo "  LaunchAgents: $LAUNCH_AGENTS"
[ -n "$SKIP_PHASES" ] && warn "SKIP_PHASES=$SKIP_PHASES"
[ "${YES_ALL:-0}" = "1" ] && warn "YES_ALL=1 — skipping pauses"

for n in 1 2 3 4 5 6 7; do
    if should_skip "$n"; then
        warn "Phase $n: SKIPPED"
        continue
    fi
    case "$n" in
        1) phase1_prereqs ;;
        2) phase2_pyenv ;;
        3) phase3_copy ;;
        4) phase4_interactive ;;
        5) phase5_launchd ;;
        6) phase6_activate ;;
        7) phase7_smoke ;;
    esac
done

echo
ok "Quickstart complete. See SKILL.md (mac variant section) for ops + slash commands."
echo
