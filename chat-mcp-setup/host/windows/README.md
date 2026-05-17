# Host setup -- Windows

Goal: a Windows desktop running both MCP servers **locally** (no LAN gateway),
each exposed at `127.0.0.1:5030/mcp` (chatlog) and `127.0.0.1:6280/mcp`
(discord-selfbot). Clients on the same machine talk to localhost; there is no
shared gateway / Bearer token (unlike the macOS host setup).

> Why no Caddy on Windows? The Windows host here is also the only client. If
> you ever need remote machines to reach it, run Caddy or Tailscale in front
> -- see [extending](#extending-to-remote-clients) at the bottom.

## Prerequisites

- Windows 10/11
- Windows microsoft-store Python is **not enough**; use `uv` (next step) which
  bundles a real Python install.
- WeChat 4.x signed in to the target account. Confirm:

```powershell
Get-Process Weixin | Format-Table Name, @{n='ver';e={$_.MainModule.FileVersionInfo.FileVersion}}
# version should be 4.x.y.z; the chatlog_alpha fork supports 4.x specifically.
```

Install `uv`:

```powershell
winget install --id=astral-sh.uv -e
# refresh PATH or open a new shell
uv --version    # should print e.g. 0.11.x
```

## Step 1 -- WeChat chatlog MCP

```powershell
# 1. Create install dir and download the chatlog_alpha Windows binary.
New-Item -ItemType Directory -Force -Path C:\Users\$env:USERNAME\chatlog | Out-Null
$url = "https://github.com/teest114514/chatlog_alpha/releases/download/latest/chatlog-windows-amd64.exe"
Invoke-WebRequest -Uri $url -OutFile "C:\Users\$env:USERNAME\chatlog\chatlog.exe"
```

> The upstream `sjzar/chatlog` project was removed by its author 2025-10-20 for
> compliance reasons. `teest114514/chatlog_alpha` is an actively maintained
> fork that adds Windows + WeChat 4.x support.

```powershell
# 2. Open a real Windows Terminal (not via SSH / not redirected) and run:
cd C:\Users\$env:USERNAME\chatlog
.\chatlog.exe
```

In the TUI (use `↑/↓ Enter`):

1. Add account (it should auto-detect your WeChat account directory under
   `%USERPROFILE%\OneDrive\Documents\xwechat_files\<account>` or
   `%USERPROFILE%\Documents\WeChat Files\<account>`).
2. Choose **"获取数据 Key" / "Get Data Key"** -- chatlog scans the running
   WeChat process and extracts the SQLite `data_key`. This writes
   `%USERPROFILE%\.chatlog\chatlog.json` with the 64-char hex key.
3. Choose **"开启 HTTP 服务" / "Start HTTP service"** -- this flips
   `http_enabled: true` in `chatlog.json` and binds `127.0.0.1:5030`.

Verify outside the TUI:

```powershell
Invoke-RestMethod http://127.0.0.1:5030/health    # -> @{status=ok}
& C:\Users\$env:USERNAME\chatlog\chatlog.exe http list | Select-Object -First 5
```

**Keep the TUI window minimized** -- chatlog is a TUI app with no headless
mode. Closing the console kills the server. Step 3 below daemon-izes it via
Task Scheduler so you don't have to keep the window open manually.

## Step 2 -- Discord self-bot MCP

```powershell
# 1. Create install dir
$dst = "C:\Users\$env:USERNAME\discord-selfbot-mcp"
New-Item -ItemType Directory -Force -Path $dst | Out-Null

# 2. Copy the Python source (cross-platform; same as macOS host).
$srcRepo = (Resolve-Path .\skills\chat-mcp-setup).Path
Copy-Item "$srcRepo\host\macos\discord-selfbot\server.py" $dst
Copy-Item "$srcRepo\host\macos\discord-selfbot\pyproject.toml" $dst

# 3. Token: same .env file as macOS, just put it at $HOME/.discord-selfbot.env
Copy-Item "$srcRepo\host\macos\discord-selfbot\dot-discord-selfbot.env" `
          "$env:USERPROFILE\.discord-selfbot.env"
icacls "$env:USERPROFILE\.discord-selfbot.env" /inheritance:r /grant:r "$env:USERNAME:(R,W)" | Out-Null

# 4. uv venv + deps (downloads CPython 3.10+ on first run)
cd $dst
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e .
```

Verify:

```powershell
.\.venv\Scripts\python.exe server.py
# should log: "Discord ready as <user> ... N guilds"
# server listening on 127.0.0.1:6280
# Ctrl+C to stop after you confirm
```

## Step 3 -- Daemon-ize via Task Scheduler

Both services should auto-start at log on. Use the bundled installer:

```powershell
& .\host\windows\scripts\install-mcp-services.ps1
```

This registers two scheduled tasks:

| Task | Window mode | Auto-start | Why |
|---|---|---|---|
| `ChatlogServer`  | Minimized   | At log on | TUI needs a real console; minimized keeps it alive in the taskbar |
| `DiscordSelfbot` | Hidden      | At log on | Plain Python, fine to fully hide + redirect stdout/stderr to log files |

To trigger now (without logging out and back in):

```powershell
Start-ScheduledTask -TaskName ChatlogServer
Start-ScheduledTask -TaskName DiscordSelfbot
Get-NetTCPConnection -LocalPort 5030,6280 -State Listen
```

## Step 4 -- Point claude mcp at localhost

```powershell
claude mcp remove --scope user chatlog        2>$null
claude mcp remove --scope user discord-selfbot 2>$null
claude mcp add --transport http --scope user chatlog         http://127.0.0.1:5030/mcp
claude mcp add --transport http --scope user discord-selfbot http://127.0.0.1:6280/mcp
claude mcp list    # both should show '✓ Connected'
```

> No Bearer header needed: services bind to loopback only.

## Things that go wrong

| Symptom | Cause | Fix |
|---|---|---|
| chatlog TUI exits immediately when run via `Start-Process -WindowStyle Hidden -RedirectStandardOutput ...` | Ink TUI detects pipes instead of a real console and bails | Use `-WindowStyle Minimized` and **no redirects** (what `install-mcp-services.ps1` does) |
| `Invoke-RestMethod http://127.0.0.1:5030/health` fails after a reboot | You logged in but ChatlogServer task hasn't fired yet, or its previous instance is still up | `Start-ScheduledTask -TaskName ChatlogServer` to force; or `Get-Process chatlog` to inspect |
| `API Error: 405` from `claude --print` in scheduled tasks | A user-scope `ANTHROPIC_BASE_URL` env var points to a proxy | `[System.Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $null, "User")` |
| WeChat history queries return 0 results from the local chatlog | Recently changed accounts or the WeChat data dir moved | Re-run the TUI, "Get Data Key", restart `ChatlogServer` |

## WeChat re-login / account change on Windows

Same rules as macOS (see `../macos/` SKILL.md section B.3a):

- **Same account re-login**: no impact, cached `data_key` stays valid.
- **Different account / WeChat reinstall / 4.x major version bump / "reset
  encryption"**: re-extract the key via TUI (`Get Data Key` menu) and restart
  the `ChatlogServer` scheduled task.

## Extending to remote clients

If you ever want another machine on your LAN (or via Tailscale) to use these
MCP servers, you can put a Caddy in front the same way the macOS host does:

```powershell
choco install caddy             # or scoop install caddy
# Mirror host/macos/mcp-gateway/Caddyfile (same syntax) and run as a
# scheduled task; add the Bearer token to client/token in this repo;
# clients then use http://<windows-ip>:7777/{chatlog,discord}/mcp like macOS.
```

Bind to `0.0.0.0:7777` only if you trust your LAN. For untrusted networks use
Tailscale and bind to the Tailscale interface address only.
