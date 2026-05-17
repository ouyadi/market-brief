---
name: market-brief-setup
description: Install or maintain "market-brief" — a scheduled task (Windows Task Scheduler or macOS launchd) that runs Claude Code hourly to scan Discord + WeChat investing chat groups and push the resulting report to the user's WeChat (primary channel) with email as fallback. Optionally also install an inbound listener so the user can chat with Claude directly from WeChat (long-poll iLink → claude --print → reply, with /ping /brief /help slash commands). Optionally also install a Playwright-based Twitter/X MCP server so Claude can fetch tweets the user's browser would see (cookies-injected headless Chrome; read-only; works around all currently-broken X scraper libs). Use when the user wants to set up hourly chat-intel automation, asks how to push AI-generated reports to WeChat, asks how to chat with Claude via WeChat, asks how to add Twitter/X intel to the pipeline, asks about combining chatlog + discord-selfbot + Hermes Agent + Claude Code into one pipeline, or wants to migrate an existing install to a new machine. Requires the chat-mcp-setup skill to have already been run (chatlog + discord-selfbot MCP servers must be reachable on localhost). Windows is the primary host (Steps 1–11); macOS is supported via `quickstart-mac.sh` + `run.sh` + `launchd/*.plist` (see macOS variant section).
---

# Market-brief setup

A pipeline that, every hour from 08:00–22:00 EDT (configurable), runs
Claude Code with a fixed prompt that:

1. Pulls the last 24h (pre-market) or 90min (intraday/after-hours) of messages
   from the user's Discord and WeChat investing groups via the local
   `mcp__chatlog__*` and `mcp__discord-selfbot__*` tools
2. Aggregates X (twitter) links cited ≥2x across distinct senders, optionally
   WebFetches them
3. Optionally extracts research-report embeds from a Discord research-bot
   channel
4. Writes a tier-tagged markdown report to `C:\Users\<u>\Reports\YYYY-MM-DD-HH-brief.md`
5. Pushes it to the user's WeChat via Hermes Agent's iLink adapter
   (split into ~3–5 chunks of ≤2000 chars each)
6. Falls back to SMTP email only if the WeChat push fails

```
Task Scheduler ─→ run.ps1 ─→ claude --print (prompt.md)
                                ↓ uses mcp__chatlog + mcp__discord-selfbot
                              YYYY-MM-DD-HH-brief.md
                                ↓
                              push_weixin.py ─→ Hermes Agent ─→ iLink ─→ user's WeChat
                                ↓ (on failure)
                              Send-MailMessage ─→ Gmail SMTP ─→ inbox
```

---

## Before you start

**Hard prerequisites**, in order:

1. **Windows 10/11 desktop that stays on during the target hours.** Task
   Scheduler cannot fire while the machine is off.
2. **`chat-mcp-setup` skill already complete** on this machine. Verify with:
   ```powershell
   Get-NetTCPConnection -LocalPort 5030,6280 -State Listen
   claude mcp list
   ```
   Both `chatlog` and `discord-selfbot` must show `✓ Connected`. If not, stop
   and run that skill first.
3. **Claude Code npm CLI** (`claude`) is installed and logged in.
4. **Long-lived Claude OAuth token** for unattended runs:
   ```powershell
   claude setup-token
   ```
   Copy the printed `sk-ant-oat01-…` — you'll paste it into `secrets.json`.
5. **Gmail account + App Password** (for the fallback email):
   - https://myaccount.google.com/security → enable 2-Step Verification
   - https://myaccount.google.com/apppasswords → generate a 16-char password
   - Outlook/Hotmail does NOT work (Microsoft disabled basic auth).
6. **WeChat 4.x logged into the target account on the same Windows machine.**
   chatlog reads its decrypted SQLite DBs; if you're not the chat owner you
   can't see the messages.

**Optional**: if the user only wants email (no WeChat push), they can skip
Steps 4–5 below entirely; the pipeline still works and `run.ps1` will go
straight to email. Most of this skill's value is the WeChat path though.

---

## Skill assets (everything you need is in this same directory)

This skill is shipped as the **standalone GitHub repo** `ouyadi/market-brief`.
After `git clone`, the repo root is the skill bundle:

```
market-brief/                       <— repo root = skill bundle
├── SKILL.md                        <— you're reading this
├── README.md                       <— GitHub landing page
├── SETUP-GUIDE.md                  <— long-form human-facing setup doc
├── LICENSE                         <— MIT
├── .gitignore                      <— excludes secrets.json / prompt.md / logs
├── prompt.template.md              <— copy → prompt.md, fill in group tables
├── run.ps1                         <— the PowerShell launcher (no changes needed for most users)
├── push_weixin.py                  <— WeChat push helper (no changes needed)
├── qr_login_bootstrap.py           <— one-time iLink QR bind
├── schedule-install.ps1            <— registers MarketBrief task
├── secrets.example.json            <— template for secrets.json
└── hermes-py.ps1                   <— legacy wrapper kept for emergency fallback
```

You will copy the runtime files (everything except SKILL.md / README.md /
LICENSE / .gitignore / .git) into the target user's
`C:\Users\<them>\Scripts\market-brief\`, then customize a few files in place.

---

## Step-by-step

### Step 0 — Confirm prerequisites with the user

Before doing any installs, ask the user to confirm:

```text
1. Are chatlog + discord-selfbot MCP servers running on this machine? (run `claude mcp list`)
2. Which timezone are you in? (run.ps1 defaults to EDT = UTC-4. Adjust if needed.)
3. Do you want the WeChat push channel, or email only?
4. Will you be ready to scan a QR with your WeChat mobile app in ~5 min?
```

If any answer is "no" or "not yet", pause and address it before continuing.

### Step 1 — Copy the skill into the user's scripts dir

```powershell
$dst = "C:\Users\$env:USERNAME\Scripts\market-brief"
if (Test-Path $dst) {
    Write-Host "ABORT: $dst already exists — inspect first; rename or remove before re-running." -ForegroundColor Red
    # do NOT clobber; user may have a working install
    exit 1
}
New-Item -ItemType Directory -Force -Path $dst | Out-Null

# Copy from this skill bundle. The skill bundle is the standalone repo
# https://github.com/ouyadi/market-brief; the user has likely already cloned
# it to $env:USERPROFILE\market-brief and Junction-linked it into
# $env:USERPROFILE\.claude\skills\market-brief-setup so Claude Code could
# discover it. Use whichever path actually holds the bundle on this machine.
$skillSrc = "$env:USERPROFILE\market-brief"   # adjust if the user cloned elsewhere
Copy-Item -Path "$skillSrc\*" -Destination $dst -Recurse `
    -Exclude @('SKILL.md', 'README.md', 'LICENSE', '.gitignore', '.git')
# SKILL.md / README.md / LICENSE stay in the skill bundle, not in the user's scripts dir.

# Make a starter prompt.md from the template
Copy-Item "$dst\prompt.template.md" "$dst\prompt.md"

# Make a starter secrets.json from the example
Copy-Item "$dst\secrets.example.json" "$dst\secrets.json"
icacls "$dst\secrets.json" /inheritance:r /grant:r "$env:USERNAME:(R,W)" | Out-Null
```

### Step 2 — Edit `prompt.md` with the user's groups

`prompt.template.md` (now copied to `prompt.md`) has two placeholder tables.
Help the user fill them:

**Discord channel IDs** — instruct the user to enable Developer Mode in
Discord (Settings → Advanced → Developer Mode), then right-click each channel
they want scanned → "Copy Channel ID" → paste into the table.

**WeChat chatroom IDs** — easier path: from the user's Claude Code session
they can call `mcp__chatlog__wx_sessions` to dump all current WeChat
sessions. Each chatroom has an ID like `25462231499@chatroom`. Pick the
investing-related ones.

Edit `prompt.md` to replace the `<服务器名>`/`<频道名>`/`<群名>` placeholder
rows with the user's actual list. Five-to-fifteen entries total is typical;
more makes Claude slower and may bust token limits.

### Step 3 — Fill `secrets.json`

Open `C:\Users\<u>\Scripts\market-brief\secrets.json` and fill in:

```json
{
  "claudeCodeOauthToken": "sk-ant-oat01-...",     // from `claude setup-token` in Step 0
  "smtpServer":  "smtp.gmail.com",
  "smtpPort":    587,
  "smtpUser":    "<user>@gmail.com",
  "smtpPassword":"abcd efgh ijkl mnop",            // 16-char App Password, NOT real password
  "fromAddress": "<user>@gmail.com",
  "toAddress":   "<user>@gmail.com"
}
```

Then verify the ACL is locked:
```powershell
icacls C:\Users\$env:USERNAME\Scripts\market-brief\secrets.json
# should show only the current user with (R,W) — no inheritance, no Authenticated Users
```

### Step 4 — Install Hermes Agent for WeChat push  *(skip if email-only)*

> **Critical gotcha to remember**: do NOT use `uv` to install Python here.
> On at least one Windows install, Defender quarantined the astral-sh
> distribution of Python a few minutes after install, breaking the venv with
> a confusing `No Python at …` error from the venv shim. Use winget-signed
> Python instead.

```powershell
# Install signed Python 3.11 system-wide (Defender does not flag this one)
winget install --id Python.Python.3.11 --source winget --silent `
    --accept-source-agreements --accept-package-agreements

$sysPy = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
$venv  = "C:\Users\$env:USERNAME\hermes-agent\.venv"
$venvPy = "$venv\Scripts\python.exe"

& $sysPy -m venv $venv
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install hermes-agent qrcode aiohttp cryptography Pillow
# NOTE: Pillow is mandatory — `qrcode.make()` uses it by default, and
# `hermes-agent` does NOT pull Pillow. Without it qr_login_bootstrap.py
# crashes with `ModuleNotFoundError: No module named 'PIL'`.

# Sanity check
& $venvPy -c "from gateway.platforms.weixin import send_weixin_direct, qr_login; print('ok')"
# expected: ok
```

### Step 5 — Bind WeChat via iLink QR  *(skip if email-only)*

This is the **only step that requires the user to physically use their phone.**

```powershell
& 'C:\Users\$env:USERNAME\hermes-agent\.venv\Scripts\python.exe' `
    'C:\Users\$env:USERNAME\Scripts\market-brief\qr_login_bootstrap.py'
```

What happens:
1. Script renders a QR PNG to `%TEMP%\hermes-qr.png` and auto-opens it with
   the user's default image viewer (usually Photos).
2. **The user must scan the on-screen QR with their phone's WeChat app**
   (NOT the desktop WeChat client — it can't scan its own screen) and tap
   confirm.
3. Script polls for confirmation; when done it prints:
   ```
   === iLink bind succeeded ===
     account_id : xxxxx@im.bot
     user_id    : xxxxx@im.wechat
     ...
   ```
   and writes credentials to `C:\Users\<u>\.hermes\.env`.

If you see `QR expired, refreshing` 2–3 times in a row and then timeout —
the user wasn't fast enough. Just re-run the command. The QR has an ~8 min
window.

After success, verify the env file:
```powershell
Get-Content $env:USERPROFILE\.hermes\.env
# should contain WEIXIN_ACCOUNT_ID, WEIXIN_TOKEN, WEIXIN_BASE_URL, WEIXIN_HOME_CHANNEL
```

### Step 6 — Smoke-test the push  *(skip if email-only)*

Push a literal short message to confirm the iLink ↔ Hermes ↔ user's WeChat
path works end-to-end:

```powershell
$venvPy = 'C:\Users\$env:USERNAME\hermes-agent\.venv\Scripts\python.exe'
& $venvPy 'C:\Users\$env:USERNAME\Scripts\market-brief\push_weixin.py' `
    --message "market-brief setup smoke test"
# expected:  OK: pushed to chat_id=...@im.wechat (message_id=hermes-weixin-...)
```

**Ask the user to confirm they received the message in WeChat** (it will look
like an incoming text from a bot named something like "小微AI" / "iLink助手"
or similar, depending on the bot's WeChat display name). Don't continue until
they confirm — if they didn't get it, debug iLink (see Troubleshooting).

### Step 7 — Full smoke-test with `-SkipEmail`

Now run the actual pipeline once, but without the fallback email:

```powershell
& 'C:\Users\$env:USERNAME\Scripts\market-brief\run.ps1' -SkipEmail
```

This takes ~3–6 minutes (Claude has to do all the MCP scanning). Expected log
output ends with one of:

- `==== market-brief run done (WeChat only) ====`  — push succeeded, all good
- `==== market-brief run done (push failed, no email) ====` — push failed,
  `-SkipEmail` suppressed the email. Debug push.
- `==== market-brief run done (email fallback) ====` — pipeline used email
  (only if `-SkipEmail` was NOT set; in this smoke test, that shouldn't
  happen).

Also verify a fresh report file exists at
`C:\Users\<u>\Reports\YYYY-MM-DD-HH-brief.md`.

### Step 8 — Register the scheduled task

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\Users\$env:USERNAME\Scripts\market-brief\schedule-install.ps1
```

Verify:
```powershell
Get-ScheduledTaskInfo -TaskName MarketBrief
# NextRunTime should be the upcoming 08:00 local time (or sooner if current
# hour is within 08–22).
(Get-ScheduledTask -TaskName MarketBrief).Actions
# Action.Arguments should point at the user's run.ps1
```

Done. Next hourly fire will run automatically.

### Step 9 — Optional: install inbound listener (chat with Claude from WeChat)

Skip if the user only wants the scheduled outbound briefs. Otherwise this
adds a long-running daemon that:

- long-polls iLink for messages the user sends to the bot
- ignores messages from anyone except the bot owner (your own `WEIXIN_HOME_CHANNEL`)
- handles slash commands cheaply (`/ping`, `/brief`, `/help`)
- forwards anything else to `claude --print --dangerously-skip-permissions`
  with a stock-intel persona system prompt, so Claude can use the same MCP
  tools (chatlog, discord-selfbot, WebFetch, etc.) you have configured
- pushes the reply back via `send_weixin_direct`

Install:

```powershell
# Register the WeixinListener scheduled task (At log on, hidden window)
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\Users\$env:USERNAME\Scripts\market-brief\install-listener.ps1

# Start immediately
Start-ScheduledTask -TaskName WeixinListener

# After ~3 seconds, verify there is exactly ONE listener python proc.
# (The PEP 405 venv shim spawns a base interpreter child — that's the
# expected parent+child pair; not two independent listeners.)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'listen_weixin' } |
    Select-Object ProcessId, ParentProcessId, ExecutablePath, CommandLine |
    Format-List
```

Smoke test:

1. In WeChat, send `/ping` to the bot. Expect reply `pong (listener up …)`
   within ~2 seconds.
2. Send `/help`. Expect command listing.
3. Send a real question in Chinese, e.g. `月哥下午说啥了`. Expect a 20s–3min
   delay (Claude is running with MCP tools) and then a Chinese answer.

Logs:

```
C:\Users\<u>\Scripts\market-brief\logs\
├── listen_weixin.log                 # listener's own structured log
├── listener.YYYY-MM-DD.out.log       # PowerShell wrapper stdout
└── listener.YYYY-MM-DD.err.log       # PowerShell wrapper stderr (parse errors land here)
```

#### Encoding pitfall to remember

`run-listener.ps1` MUST be pure ASCII (no em-dashes, no smart quotes, no
Chinese). When Task Scheduler spawns `powershell.exe -File <script>`, the new
PowerShell process reads the .ps1 with system codepage if there is no BOM —
non-ASCII bytes break quote pairing and the script fails to parse. Logs land
in `listener.YYYY-MM-DD.err.log`. Stick to ASCII; build any Chinese strings
in memory from `[byte[]](...)` + `UTF8.GetString`.

#### Listener vs scheduled-outbound coexistence

Both share the same iLink token but use different endpoints (long-poll
`/getupdates` for inbound, `/sendmessage` for outbound). They do NOT contend
for the long-poll exclusive lock unless you spin up a *second* listener
process. If you ever see the listener replying twice or messages disappearing,
check for stale `listen_weixin.py` processes with the CIM query above and
kill them before restarting the task.

### Step 10 — Optional: Twitter/X MCP via Playwright

Skip unless the user wants Claude to fetch X content for them (either
during market-brief runs or via WeChat ad-hoc questions). Otherwise the
pipeline falls back to bare `WebFetch` which hits the X login wall ~40%
of the time.

**Important caveats — make sure user understands before installing:**

- **Uses the user's main X account cookies.** All `mcp__twitter__*` tools
  are read-only by design, but the cookies could in principle authorize
  writes; if X's anti-bot ever flags this server, the user's main account
  is at risk. Acceptable for low-frequency hourly reads; not acceptable
  if the user is paranoid about that account.
- **Requires user-installed Google Chrome** at `C:\Program Files\Google\Chrome\Application\chrome.exe`.
  Playwright's bundled Chromium does NOT work from scheduled-task spawn
  context on Windows (same Defender-quarantine-like symptom as uv-managed
  Python; same fix — use Chrome instead).
- **All major Twitter scraper libs are currently broken.** I tested
  `agent-twitter-client` (deprecated, dead) and `twscrape` (active,
  also dead with systemic IndexError as of 2026-05). Don't waste hours
  there — go straight to Playwright.

Install sequence (assumes hermes-agent venv from Step 4 is already up):

```powershell
# 1. Install Playwright + Chromium binary (full chromium needed at
#    install-time even though daemon uses user-installed Chrome)
$venvPy = 'C:\Users\$env:USERNAME\hermes-agent\.venv\Scripts\python.exe'
& $venvPy -m pip install playwright mcp
& $venvPy -m playwright install chromium

# 2. Create ~/twitter-mcp with .env (cookies extracted from Chrome DevTools)
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\twitter-mcp"
# .env shape (cookie values are from Chrome F12 → Application → Cookies → x.com,
# but use Domain=.twitter.com because some libs key on that):
# AUTH_METHOD=cookies
# TWITTER_COOKIES=["auth_token=...; Domain=.twitter.com","ct0=...; Domain=.twitter.com","twid=...; Domain=.twitter.com"]
# PORT=3030
icacls "$env:USERPROFILE\twitter-mcp\.env" /inheritance:r /grant:r "$env:USERNAME:(R,W)"

# 3. Copy twitter_playwright_mcp.py + install-twitter-mcp.ps1 into ~/twitter-mcp
# (these are in this repo)

# 4. Register the daemon
& "$env:USERPROFILE\twitter-mcp\install-twitter-mcp.ps1"

# 5. Start immediately
Start-ScheduledTask -TaskName TwitterMCP

# 6. Register the MCP with claude
claude mcp add --transport http --scope user twitter http://127.0.0.1:3031/mcp

# 7. Verify (after ~5s for Chrome warm-up)
claude mcp list   # twitter should be ✓ Connected
```

Smoke test through claude (this will spawn headless Chrome ~5s + load
CNBC page ~3s on first call):

```powershell
# In a shell with CLAUDE_CODE_OAUTH_TOKEN loaded from secrets.json
"用 mcp__twitter__fetch_user_tweets cnbc limit=2" | claude --print --dangerously-skip-permissions
# Expected: 2 tweet text/time/author dicts from @CNBC
```

#### Why `channel='chrome'` (not bundled Chromium)

Inside `twitter_playwright_mcp.py` the launch call is:
```python
await _playwright.chromium.launch(headless=True, channel="chrome", ...)
```
Even though we ran `playwright install chromium`, the daemon **cannot see**
files under `%LOCALAPPDATA%\ms-playwright\` from the scheduled-task spawn
context — `os.path.exists()` returns False on a file `bash find` confirms
exists. Exact same symptom as the uv-managed Python issue. Workaround:
use user-installed Chrome (in `C:\Program Files\Google\Chrome\...`) which
is in a Defender-trusted path.

#### Read-only enforcement

The wrapper deliberately exposes only `fetch_*` and `search_*` tools.
The listener's SYSTEM_PROMPT also includes an explicit read-only warning.
Two independent layers. Do NOT add `post_tweet` / `like` / `follow`
without explicit user re-consent — main-account ban risk.

#### What gets logged where

- `~/twitter-mcp/logs/twitter_mcp.log` — structured wrapper log (tool calls)
- `~/twitter-mcp/logs/twitter-mcp.YYYY-MM-DD.{out,err}.log` — daemon wrapper PowerShell stdout/stderr

### Step 11 — Optional: Stock price MCP via yfinance

Skip if the user only cares about chat-group intel without quantitative
price/fundamentals overlay. Otherwise this is the cheapest of the optional
MCPs to install (no auth, no Defender path) and unlocks the **enriched ⚡
section in pre-market briefs** (Step 5c in prompt.md) plus the **/plan**
slash command.

```powershell
# 1. Install yfinance + mcp into the existing hermes-agent venv
$venvPy = "C:\Users\$env:USERNAME\hermes-agent\.venv\Scripts\python.exe"
& $venvPy -m pip install yfinance mcp

# 2. Copy MCP wrapper + installer into ~/stock-mcp
$dst = "$env:USERPROFILE\stock-mcp"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item .\stock_price_mcp.py "$dst\"
Copy-Item .\install-stock-mcp.ps1 "$dst\"

# 3. Register as scheduled task (At log on, hidden window)
& "$dst\install-stock-mcp.ps1"
Start-ScheduledTask -TaskName StockPriceMCP

# 4. Register with claude
claude mcp add --transport http --scope user stock-price http://127.0.0.1:3032/mcp
claude mcp list   # should show 'stock-price ✓ Connected'
```

Smoke test:

```powershell
# In a shell with CLAUDE_CODE_OAUTH_TOKEN loaded
"用 mcp__stock-price__get_quote ticker='NVDA'" | claude --print --dangerously-skip-permissions
# expected: NVDA price + 52w range + market cap
```

Tools exposed:
- `get_quote(ticker)` — current snapshot
- `get_history(ticker, period, interval)` — OHLCV bars (caps 50 most recent)
- `get_info(ticker)` — sector / forward_pe / next_earnings_date / dividend / MAs
- `check_post_hoc(ticker, at_time, horizon)` — event-study micro: at a tweet/message timestamp + N-period horizon, returns price-at-time, max gain/drawdown, net move. **Use to grade KOL/group call accuracy**.

Caveats:
- yfinance scrapes Yahoo Finance, so high-freq calls (>10/min sustained)
  may hit transient rate limits. Hourly market-brief usage is fine.
- Does NOT hit the Defender file-visibility issue that Playwright did —
  pure HTTP, no browser binary.

---

## Operational cheat sheet (give to user)

```powershell
# Force a run now
Start-ScheduledTask    -TaskName MarketBrief

# Inspect
Get-ScheduledTaskInfo  -TaskName MarketBrief

# Latest report
Get-Content (Get-ChildItem "C:\Users\$env:USERNAME\Reports\$(Get-Date -Format yyyy-MM-dd)-*-brief.md" |
             Sort-Object Name | Select-Object -Last 1)

# Today's log
Get-Content "C:\Users\$env:USERNAME\Scripts\market-brief\logs\$(Get-Date -Format yyyy-MM-dd).log" -Tail 30

# Manual run, no fallback email
& "C:\Users\$env:USERNAME\Scripts\market-brief\run.ps1" -SkipEmail

# Re-bind WeChat (token rotated, device change, session permanently broken)
& "C:\Users\$env:USERNAME\hermes-agent\.venv\Scripts\python.exe" `
  "C:\Users\$env:USERNAME\Scripts\market-brief\qr_login_bootstrap.py"

# Inspect current iLink creds
Get-Content $env:USERPROFILE\.hermes\.env

# Stop the schedule
Unregister-ScheduledTask -TaskName MarketBrief -Confirm:$false
```

### Slash commands (via the listener in WeChat)

After the listener is up (Step 9), the user can DM the bot from WeChat with:

| Command | What it does |
|---|---|
| `/ping` | Check listener is alive |
| `/brief` | Trigger one `MarketBrief` run immediately |
| `/dv [handle] [Nh]` | KOL digest. `/dv` = all KOLs × 2h; `/dv cathiedwood 24h` = single handle, custom window |
| `/xfeed [tab] [N]` | X home timeline brief. `/xfeed` = For You + Following each 15; `/xfeed for_you`; `/xfeed following 25` |
| `/plan [tickers]` | Enriched execution plan (stock-price + X live news + tech levels + entry/stop/target). `/plan` = top 3 from latest brief; `/plan TSLA NVDA` = specific tickers |
| `/ticker TICKER [Nh] [top\|live]` | Single-ticker X discussion via cashtag. `/ticker SKM 6h` / `/ticker NVDA 30 top` |
| `/help` | List all commands |
| (any other text) | Free-form question → Claude with full MCP access |

Config management (also via WeChat, free-form):
- `加进监控 群名,...` / `删群 群名` — manages the WeChat group / Discord channel tables in `prompt.md`
- `大 V 加 handle,...` / `大 V 删 handle` — manages the KOL table
- `个股加 SKM, INTC, NVDA` / `个股删 SKM` — manages the watchlist
Listener Claude does the wx_sessions / list_channels / fetch_user_tweets / get_info lookups + edits `prompt.md` itself.

### MCPs registered at the end

```text
chatlog           http://127.0.0.1:5030/mcp   WeChat chat history (from chat-mcp-setup)
discord-selfbot   http://127.0.0.1:6280/mcp   Discord channel history (from chat-mcp-setup)
twitter           http://127.0.0.1:3031/mcp   X/Twitter via Playwright + cookies (Step 10)
stock-price       http://127.0.0.1:3032/mcp   yfinance: quote/history/info/check_post_hoc (Step 11)
```

---

## iLink session model (must explain to user once)

iLink has a per-session quota system that the user needs to understand,
because the maintenance pattern is non-obvious:

- When the user **sends a message into the bot's WeChat chat from their
  phone**, iLink opens (or refreshes) an outbound session for that user.
- Within a session, the bot can push ~10 messages outward before quota
  exhausts.
- One market-brief run = ~3–5 chunks. So a fresh session covers ~2–3 runs.
- The user touching the bot chat in WeChat at any time resets the quota.

Hermes' adapter handles single-shot expiry transparently (on `ret=-14` it
clears the stored `context_token` and retries once without it, which usually
re-opens the session). But if a full session genuinely runs out and the user
isn't interacting with the bot, every hourly run will fail to push and
fall through to email.

**Operational signal**: if the user starts getting hourly fallback emails
where they previously got only WeChat messages, tell them to either (a) send
any text to the bot in WeChat, or (b) re-run `qr_login_bootstrap.py` to get
a fresh token.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `API Error: 405` from claude --print | Claude Desktop injected `ANTHROPIC_BASE_URL` env var pollutes the child | `run.ps1` already strips a long list of these; add new ones if discovered |
| `chatlog wx_history` timing out at 30s | Upstream MCP wrapper has a hardcoded 30s timeout | Use the `ouyadi/chatlog_alpha` fork with PR #64 applied — see chat-mcp-setup skill |
| chatlog returns empty messages | Built with `CGO_ENABLED=0`, sqlite3 is a stub | Rebuild with CGO=1 + MinGW gcc; binary should be ~72 MB not 47 MB |
| `No Python at …` from venv | Defender quarantined uv-managed Python | Use `winget install Python.Python.3.11` (signed) instead of `uv venv --python` |
| `ModuleNotFoundError: PIL` in qr_login_bootstrap.py | `hermes-agent` core doesn't pull Pillow | `pip install Pillow` into the venv |
| Gmail SMTP `5.7.139 Authentication unsuccessful` | Using a real Gmail password instead of App Password | Generate App Password at myaccount.google.com/apppasswords |
| Outlook SMTP also fails | Microsoft disabled basic auth | Switch to Gmail; Outlook requires OAuth2 which Send-MailMessage doesn't speak |
| Hourly fallback emails suddenly start | iLink session exhausted (~10 quota) | Have user touch the bot chat in WeChat to refresh, or re-bind via Step 5 |
| Run logs show `rate limited; backing off Ns` | Normal — Hermes adapter is self-retrying iLink rate-limit responses | No action needed |
| PowerShell parse error on `.ps1` containing Chinese | UTF-8 BOM vs UTF-16 detection bug | Keep `.ps1` pure ASCII; build Chinese strings from `[byte[]]` + `UTF8.GetString()` in memory (run.ps1 already does this for the email subject) |
| Task fires at 14:59:58 and writes to 14:xx file | Windows clock skew | run.ps1 already adds `AddSeconds(30)` slop |
| OneDrive makes chatlog data cloud-only | Files-On-Demand default | Run `attrib +P` on the WeChat data dir to pin to local |

---

## macOS variant

The Mac port is shipped as a parallel set of files in the same repo. The
Python helpers (`push_weixin.py`, `qr_login_bootstrap.py`, `listen_weixin.py`,
`twitter_playwright_mcp.py`, `stock_price_mcp.py`) are now cross-platform —
they resolve paths from `Path.home()` instead of hardcoded `C:\Users\...`.
Only the launchers and scheduler glue differ.

### File mapping (Windows ↔ macOS)

| Purpose | Windows | macOS |
|---|---|---|
| Hourly pipeline launcher | `run.ps1` | `run.sh` |
| Scheduler registration | `schedule-install.ps1` (Task Scheduler) | `launchd/com.ouyadi.market-brief.plist` |
| Listener daemon | `install-listener.ps1` (Task Scheduler) | `launchd/com.ouyadi.weixin-listener.plist` |
| Twitter MCP daemon | `install-twitter-mcp.ps1` | `launchd/com.ouyadi.twitter-mcp.plist` |
| Stock-price MCP daemon | `install-stock-mcp.ps1` | `launchd/com.ouyadi.stock-mcp.plist` |
| One-command installer | `quickstart.ps1` | `quickstart-mac.sh` |

### Mac one-command install

```bash
# From the cloned repo root:
bash quickstart-mac.sh
# or skip phases:
SKIP_PHASES="4,5" bash quickstart-mac.sh
```

Same 7 idempotent phases as Windows (prereqs → copy → secrets → Hermes venv
→ QR bind → register launchd jobs → smoke test). Pauses for the same 4
manual checkpoints (edit prompt.md, edit secrets.json, scan WeChat QR, paste
X cookies). User-installed Chrome must exist at
`/Applications/Google Chrome.app` for the Twitter MCP (same Defender-style
reasoning as Windows: Playwright's bundled Chromium occasionally fails to
launch from launchd's spawn context — using Chrome.app sidesteps it).

### Manual launchd install (if not using quickstart-mac.sh)

```bash
# 1. Copy runtime files into ~/Scripts/market-brief (mirror of Windows layout)
mkdir -p ~/Scripts/market-brief
rsync -a --exclude SKILL.md --exclude README.md --exclude LICENSE \
      --exclude .git --exclude launchd ./ ~/Scripts/market-brief/
cp prompt.template.md ~/Scripts/market-brief/prompt.md
cp secrets.example.json ~/Scripts/market-brief/secrets.json
chmod 600 ~/Scripts/market-brief/secrets.json

# 2. Render plists with the user's $HOME baked in (launchd does not expand $HOME)
mkdir -p ~/Library/LaunchAgents
for f in launchd/*.plist; do
    sed "s|\$HOME|$HOME|g" "$f" > ~/Library/LaunchAgents/$(basename "$f")
done

# 3. Load all four
for label in market-brief weixin-listener twitter-mcp stock-mcp; do
    launchctl unload ~/Library/LaunchAgents/com.ouyadi.$label.plist 2>/dev/null
    launchctl load ~/Library/LaunchAgents/com.ouyadi.$label.plist
done

# 4. Verify
launchctl list | grep ouyadi
# expect: com.ouyadi.market-brief, ...weixin-listener, ...twitter-mcp, ...stock-mcp
```

### Mac-specific paths & gotchas

- **launchd does NOT expand `$HOME` inside `<string>` tags.** Either bake the
  literal path in via `sed` (as above), or wrap the command in `bash -c` and
  reference `$HOME` from inside the shell (which the shipped plists do).
- **Hour scheduling is local-time per launchd.** The market-brief plist sets
  `TZ=America/New_York` in `EnvironmentVariables` so that `run.sh`'s "8–22
  EDT" check matches the system clock regardless of the user's actual tz.
  If the user is in PT and wants local-time-based briefs, edit the plist to
  remove the TZ var.
- **WeChat 4.x on macOS is unverified by chatlog.** The chat-mcp-setup
  skill's `host/macos/` covers WeChat 3.x decryption; WeChat 4.x on Mac has
  not been tested against the chatlog fork yet. If the user is on WeChat
  4.x macOS, expect to debug — chatlog DB key extraction may have shifted.
  Discord-selfbot works identically.
- **No Defender, but Gatekeeper.** First launch of `chatlog` binary may
  prompt for "open from unidentified developer" — right-click → Open once
  to approve.
- **Email fallback works as-is.** `run.sh` uses the same `smtplib` Python
  one-liner via the venv, no `Send-MailMessage` PowerShell equivalent needed.

### Operational cheat sheet (macOS)

```bash
# Force a run now
launchctl kickstart -k gui/$(id -u)/com.ouyadi.market-brief

# Latest report
ls -t ~/Reports/$(date +%Y-%m-%d)-*-brief.md | head -1 | xargs cat

# Today's log
tail -30 ~/Scripts/market-brief/logs/$(date +%Y-%m-%d).log

# Tail daemon stdout/stderr
tail -f /tmp/com.ouyadi.weixin-listener.{out,err}

# Manual run, no fallback email
~/Scripts/market-brief/run.sh --skip-email

# Restart a daemon after editing the plist
launchctl unload ~/Library/LaunchAgents/com.ouyadi.weixin-listener.plist
launchctl load   ~/Library/LaunchAgents/com.ouyadi.weixin-listener.plist

# Stop the schedule entirely
launchctl unload ~/Library/LaunchAgents/com.ouyadi.market-brief.plist
```

---

## What to skip / what to do if user just wants email

If the user explicitly says "I don't want WeChat push, just email":

- Skip Steps 4, 5, 6 entirely.
- In Step 7, the smoke test will see `push_weixin.py` missing or
  hermes-agent venv missing, log a WARN, and fall through to email. That's
  the intended behavior.
- In `secrets.json`, the SMTP fields are still mandatory.

The pipeline degrades to "claude → email" cleanly without code changes.
