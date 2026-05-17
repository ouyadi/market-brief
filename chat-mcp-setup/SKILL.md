---
name: chat-mcp-setup
description: Set up Discord self-bot MCP and WeChat chatlog MCP. Two deployment shapes are supported - a Mac running both servers behind a LAN gateway (clients use http://<gateway-host>:7777/), and a Windows desktop running both servers locally on 127.0.0.1:5030 / 127.0.0.1:6280. Use when the user wants to register `mcp__discord-selfbot__*` or `mcp__chatlog__wx_*` tools on a new computer, copy chat MCP access to another machine, troubleshoot connectivity, or rebuild the host-side services (chatlog binary, discord-selfbot Python service, optional Caddy gateway).
---

# Chat MCP setup

There are two supported host platforms; pick one. Clients are configured the
same way in either case (see §A).

### Shape 1 — macOS host with LAN gateway

```
   Client Mac  ─── http://<gateway-host>:7777/discord/mcp  ──╮
   Client Mac  ─── http://<gateway-host>:7777/chatlog/mcp  ──┤
                                                           ▼
                            ┌──── Caddy :7777 (Bearer auth) ────┐
                            │   /discord/* → 127.0.0.1:6280      │
                            │   /chatlog/* → 127.0.0.1:5030      │
                            └────────────────────────────────────┘
                                          host Mac
```

The shared Bearer token lives at `client/token` (and
`host/macos/mcp-gateway/token`). Build/maintain in §B.macOS.

### Shape 2 — Windows host, local services (no gateway)

```
   Same Windows machine (no LAN, no Bearer, no Caddy):

       claude mcp on this machine ─── http://127.0.0.1:5030/mcp  (chatlog)
                                  └── http://127.0.0.1:6280/mcp  (discord-selfbot)

   Both services run as scheduled tasks (At log on):
     ChatlogServer  - chatlog.exe   minimized window  (TUI needs a real console)
     DiscordSelfbot - python server.py  hidden window (plain Python, stdio redirected)
```

Build/maintain in §B.windows. The Windows host setup is the lower-overhead
option (no Bearer rotation, no LAN exposure) but only serves clients on the
same machine.

**No live credentials are tracked in git.** `host/macos/mcp-gateway/token`,
`client/token`, `host/macos/discord-selfbot/dot-discord-selfbot.env`, and
`host/macos/chatlog/chatlog.json` are all `.gitignore`'d (see
[`.gitignore`](.gitignore)). The repo carries only `.example` placeholders;
each host fills in real values locally and never commits them back.

---

> **Note**: 2026-05 从私 repo `ouyadi/mcp-chat-skills` 迁入 `ouyadi/market-brief`(本仓 `chat-mcp-setup/` 子目录)+ scrub 了所有个人 token / data_key / LAN IP。


## A. Client setup

### A.1 — Connecting to the macOS LAN gateway

On any new Mac/Linux on the same LAN:

```bash
# 1. Clone market-brief (chat-mcp-setup lives at its top level)
git clone https://github.com/ouyadi/market-brief.git ~/market-brief

# 2. Drop the shared Bearer token into client/token (NOT committed — get it from
#    the gateway host out-of-band, e.g. scp or a password manager).
echo 'YOUR_BEARER_TOKEN_HERE' > ~/market-brief/chat-mcp-setup/client/token
chmod 600 ~/market-brief/chat-mcp-setup/client/token

# 3. Make Claude Code discover this skill
mkdir -p ~/.claude/skills
ln -s ~/market-brief/chat-mcp-setup ~/.claude/skills/chat-mcp-setup

# 4. Register both MCP servers with the LAN gateway
#    (override GATEWAY_HOST since the placeholder in install-client.sh is <gateway-host>)
GATEWAY_HOST=192.168.x.y ~/market-brief/chat-mcp-setup/client/install-client.sh

# 5. Restart Claude Code, open any session, run /mcp
#    Both 'discord-selfbot' and 'chatlog' should be 'connected'.
```

The `install-client.sh` script:
1. Reads the shared token from `client/token`.
2. Probes `http://<gateway-host>:7777/discord/mcp` and expects a `401` (proves
   gateway reachable, auth-protected).
3. Runs `claude mcp add --transport http --scope user <name> <url> --header "Authorization: Bearer …"`
   for each of the two endpoints, writing to `~/.claude.json`. (Positionals must
   come before `--header`; the flag is variadic and will swallow them otherwise.)

Override host/port via env if the host's address changes:

```bash
GATEWAY_HOST=<gateway-lan-ip> GATEWAY_PORT=7777 ~/market-brief/.../install-client.sh
```

What ends up in `~/.claude.json`:

```json
"mcpServers": {
  "discord-selfbot": {
    "type": "http",
    "url": "http://<gateway-host>:7777/discord/mcp",
    "headers": { "Authorization": "Bearer <token>" }
  },
  "chatlog": {
    "type": "http",
    "url": "http://<gateway-host>:7777/chatlog/mcp",
    "headers": { "Authorization": "Bearer <token>" }
  }
}
```

### Client-side troubleshooting

```bash
# 1. Gateway reachable at all?
nc -zv <gateway-host> 7777

# 2. Gateway answering with auth required?
curl -s -o /dev/null -w '%{http_code}\n' http://<gateway-host>:7777/discord/mcp
# expected: 401

# 3. Auth accepted?
TOKEN=$(cat ~/market-brief/chat-mcp-setup/client/token)
curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
  http://<gateway-host>:7777/discord/mcp
# expected: 200 with an MCP initialize response
```

Common failures:
- `nc` fails → wrong Wi-Fi, host asleep, or LAN IP changed. Confirm the host's
  current IP with `arp -a | grep -i weidi` from another machine, or run
  `ipconfig getifaddr en0` on the host.
- `nc` succeeds but `401` keeps coming with the token → repo on this client is
  stale; the host rotated the token. Pull the repo again.
- `200` from curl but Claude Code still shows `failed` → `claude mcp add` was
  run from the wrong scope. Re-run `install-client.sh` (it rewrites user scope)
  and restart Claude Code.

### A.2 — Local-only on a Windows host

If you ran §B.windows on this same Windows machine, there's no gateway and no
shared token. Just point claude at localhost:

```powershell
claude mcp remove --scope user chatlog        2>$null
claude mcp remove --scope user discord-selfbot 2>$null
claude mcp add --transport http --scope user chatlog         http://127.0.0.1:5030/mcp
claude mcp add --transport http --scope user discord-selfbot http://127.0.0.1:6280/mcp
claude mcp list   # both should be ✓ Connected
```

Troubleshooting for the Windows host setup itself (services not starting,
chatlog TUI exit on hidden window, etc.) lives in
[`host/windows/README.md`](host/windows/README.md#things-that-go-wrong).

---

## B.macOS — Host setup (rebuilding `<gateway-host>`)

> All assets for this path live under `host/macos/`. Skip to §B.windows if
> you're using a Windows host instead.

Only needed if the host Mac is being reinstalled or replaced. Three components,
in order: Discord MCP → Chatlog MCP → Gateway.

### B.1 Prerequisites on the host

```bash
# Tools
curl -LsSf https://astral.sh/uv/install.sh | sh        # uv
brew install caddy gh                                  # caddy for gateway, gh for repo

# Sanity
sw_vers -productVersion          # macOS
ls "$HOME/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files" \
  && echo "WeChat data dir present"
```

### B.2 Discord self-bot MCP (loopback only)

```bash
mkdir -p ~/discord-selfbot-mcp
cp -R host/macos/discord-selfbot/. ~/discord-selfbot-mcp/

# Real burner token already inside; rename and lock down
mv ~/discord-selfbot-mcp/dot-discord-selfbot.env ~/.discord-selfbot.env
chmod 600 ~/.discord-selfbot.env

cd ~/discord-selfbot-mcp
uv venv .venv
uv pip install --python .venv/bin/python -e .
./install-daemon.sh                # per-user LaunchAgent, no sudo
nc -z 127.0.0.1 6280 && echo "discord MCP up"
```

The token in `~/.discord-selfbot.env` is a **burner-account user token**.
Self-botting violates Discord ToS; do not point this at your real account.

### B.3 WeChat chatlog MCP (loopback only)

```bash
mkdir -p ~/chatlog
# Binary is 34MB Go, not in this repo — pull from upstream:
curl -L -o ~/chatlog/chatlog \
    "https://github.com/sjzar/chatlog/releases/latest/download/chatlog-darwin-arm64"
chmod +x ~/chatlog/chatlog

cp host/macos/chatlog/com.chatlog.daemon.plist  ~/chatlog/
cp host/macos/chatlog/install-daemon.sh         ~/chatlog/
cp host/macos/chatlog/uninstall-daemon.sh       ~/chatlog/
chmod +x ~/chatlog/install-daemon.sh ~/chatlog/uninstall-daemon.sh

mkdir -p ~/.chatlog
cp host/macos/chatlog/chatlog.json ~/.chatlog/chatlog.json
# data_key / img_key inside chatlog.json are bound to this account on this Mac;
# regenerate via `chatlog` CLI if WeChat is reinstalled or the account changes.

sudo ~/chatlog/install-daemon.sh   # system LaunchDaemon (root needs to read WeChat container)
curl -sS http://127.0.0.1:5030/api/v1/ping
```

> **Why this plist sets `HOME=$HOME` even though the daemon runs as root:**
> chatlog uses `os.UserHomeDir()` to find its config (`$HOME/.chatlog/chatlog.json`).
> If `HOME` were the default `/var/root`, chatlog would silently start with
> defaults (`http_enabled=false`), the TUI would come up, but `:5030` would
> never listen. Pointing `HOME` at the real user is what makes
> `http_enabled: true` in the user's chatlog.json actually take effect.

### B.3a WeChat re-login / account change — when chatlog breaks

**Same WeChat account re-login** (logout → log back in as the same user):
no impact. The local SQLite `data_key` is bound to the data directory, not the
session, so the cached key in `chatlog.json` stays valid. chatlog continues
hooking new messages.

**chatlog appears broken** if any of these happened on the host:
- logged into a *different* WeChat account (new account ID → new data_dir,
  old data_key useless)
- WeChat was reinstalled (container UUID may rotate)
- WeChat 4.x major version bump (sometimes rotates encryption)
- "Clear chat data" / "Reset encryption" was used in WeChat

Symptoms: `chatlog.log` shows `decrypt failed`, or
`curl http://127.0.0.1:5030/api/v1/sessions?format=json` returns empty / errors.

Recovery — re-extract the data_key:

```bash
# 1. Make sure WeChat is running and logged in to the target account
# 2. Drive chatlog interactively to extract the new key.
#    In the TUI, navigate to 'Get Data Key' (回车 to confirm). It writes the
#    new data_key and img_key into ~/.chatlog/chatlog.json.
sudo $HOME/chatlog/chatlog

# 3. Restart the daemon to pick up the new key
sudo launchctl kickstart -k system/com.chatlog.daemon

# 4. Verify
curl -sS 'http://127.0.0.1:5030/api/v1/sessions?limit=1&format=json' | head -c 200
```

> ⚠️ **Do NOT commit `~/.chatlog/chatlog.json` back into this repo.**
> `market-brief` is a public repo and `chatlog.example.json` (committed) is
> the only chatlog config that lives in git. The real `data_key` / `img_key`
> stay in `~/.chatlog/chatlog.json` on each host. For a second host that
> needs the same WeChat data, drive the chatlog TUI on that host
> independently — the key is derived per-machine and isn't portable anyway.

If the WeChat account ID itself changed, also update `account` and
`last_account` in `~/.chatlog/chatlog.json` (and the `data_dir` / `work_dir`
paths derived from it) before restarting the daemon.

### B.4 LAN gateway (Caddy + Bearer)

```bash
mkdir -p ~/mcp-gateway
cp host/macos/mcp-gateway/Caddyfile                       ~/mcp-gateway/
cp host/macos/mcp-gateway/com.mcp-gateway.daemon.plist    ~/mcp-gateway/
cp host/macos/mcp-gateway/token                            ~/mcp-gateway/token
cp host/macos/mcp-gateway/install-daemon.sh                ~/mcp-gateway/
chmod 600 ~/mcp-gateway/token
chmod +x ~/mcp-gateway/install-daemon.sh

~/mcp-gateway/install-daemon.sh    # per-user LaunchAgent, no sudo
# Smoke test
TOKEN=$(cat ~/mcp-gateway/token)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7777/discord/mcp
# expected: 401
curl -s -o /dev/null -w '%{http_code}\n' \
    -X POST -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
    http://127.0.0.1:7777/discord/mcp
# expected: 200
```

macOS will prompt to allow incoming connections to `caddy` on the first LAN
hit — accept it, or pre-approve with:

```bash
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/bin/caddy
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp /opt/homebrew/bin/caddy
```

### B.5 Rotating the token

```bash
openssl rand -hex 32 > ~/mcp-gateway/token
chmod 600 ~/mcp-gateway/token
~/mcp-gateway/install-daemon.sh    # re-bakes the token into the plist + reloads

# Distribute the new token to each client out-of-band (scp, 1Password, etc.)
# — DO NOT commit it. The repo is public; only .example templates are tracked.
# On each client:
#   echo 'NEW_TOKEN' > ~/market-brief/chat-mcp-setup/client/token
#   ~/market-brief/chat-mcp-setup/client/install-client.sh
```

---

## B.windows — Host setup (Windows desktop, local services)

> All assets for this path live under [`host/windows/`](host/windows/).
> See the full step-by-step in [`host/windows/README.md`](host/windows/README.md);
> the section below is just the executive summary.

Differences vs. macOS host:

- **No Caddy / no Bearer token.** Services bind to `127.0.0.1`; clients on the
  same machine call them directly. If you ever need remote clients, drop Caddy
  in front the same way macOS does -- see `host/windows/README.md` extending
  section.
- **chatlog**: download `chatlog-windows-amd64.exe` from
  [`teest114514/chatlog_alpha`](https://github.com/teest114514/chatlog_alpha)
  (active fork of the deleted `sjzar/chatlog`; supports WeChat 4.x on
  Windows). The data_key has to be extracted **interactively via the TUI** on
  each new Windows host -- it isn't portable from the macOS chatlog.json.
- **discord-selfbot**: same Python source (`server.py`, `pyproject.toml`,
  `dot-discord-selfbot.env`) as macOS; just put them at
  `C:\Users\<you>\discord-selfbot-mcp\` and run via `uv`. The Discord burner
  token is shared between both deployment shapes.
- **Daemon-ization**: two Windows Scheduled Tasks (At-Log-On trigger):
  - `ChatlogServer`  -- launches `chatlog.exe` with `-WindowStyle Minimized`
    (the Ink TUI needs a real console; hidden + stdio-redirected makes it
    exit immediately).
  - `DiscordSelfbot` -- launches the Python `server.py` with
    `-WindowStyle Hidden` and stdio redirected to log files (plain Python is
    happy without a TTY).

  Both registered by [`host/windows/scripts/install-mcp-services.ps1`](host/windows/scripts/install-mcp-services.ps1).

Quickstart (after `git clone` of this repo to e.g. `C:\Users\<you>\market-brief`):

```powershell
winget install --id=astral-sh.uv -e         # Python package manager + Python install

# chatlog
mkdir C:\Users\$env:USERNAME\chatlog
Invoke-WebRequest `
    "https://github.com/teest114514/chatlog_alpha/releases/download/latest/chatlog-windows-amd64.exe" `
    -OutFile C:\Users\$env:USERNAME\chatlog\chatlog.exe
# Run TUI ONCE in a real Windows Terminal:
#   cd C:\Users\$env:USERNAME\chatlog ; .\chatlog.exe
#   -> 解密数据 -> 开启 HTTP 服务

# discord-selfbot
$src = "C:\Users\$env:USERNAME\market-brief\chat-mcp-setup\host\macos\discord-selfbot"
$dst = "C:\Users\$env:USERNAME\discord-selfbot-mcp"
mkdir $dst
Copy-Item "$src\server.py","$src\pyproject.toml" $dst
Copy-Item "$src\dot-discord-selfbot.env" "$env:USERPROFILE\.discord-selfbot.env"
icacls "$env:USERPROFILE\.discord-selfbot.env" /inheritance:r /grant:r "$env:USERNAME:(R,W)" | Out-Null
cd $dst ; uv venv .venv ; uv pip install --python .venv\Scripts\python.exe -e .

# Daemon-ize (At log on)
& C:\Users\$env:USERNAME\market-brief\chat-mcp-setup\host\windows\scripts\install-mcp-services.ps1
Start-ScheduledTask -TaskName ChatlogServer
Start-ScheduledTask -TaskName DiscordSelfbot

# Verify
Get-NetTCPConnection -LocalPort 5030,6280 -State Listen
```

Refer to [`host/windows/README.md`](host/windows/README.md) and
[`host/windows/chatlog/README.md`](host/windows/chatlog/README.md) for
full setup, key-rotation notes, and the alpha-version caveats.

---

## C. Security boundaries

- **No real credentials live in this (public) repo.** Only `.example`
  templates are committed. The `.gitignore` at `chat-mcp-setup/.gitignore`
  blocks the live files (`client/token`, `host/macos/mcp-gateway/token`,
  `host/macos/discord-selfbot/.env`, `host/macos/chatlog/chatlog.json`).
- Live credentials sit on each host outside git:
  - Discord burner token → `host/macos/discord-selfbot/.env` (local)
  - Gateway Bearer → `host/macos/mcp-gateway/token` + `client/token` (local)
  - WeChat `data_key` / `img_key` → `~/.chatlog/chatlog.json` (per-machine,
    extracted via the chatlog TUI; never copied between machines)
- If any of them leaks anyway:
  1. Rotate the gateway token (macOS host §B.macOS.5) — invalidates LAN access
     immediately. Windows hosts don't have a gateway token to rotate.
  2. Log out everywhere on the Discord burner — invalidates that user token.
  3. Change the WeChat account password to invalidate the `data_key`, then
     re-extract via the chatlog TUI.
- **macOS host:** the Caddy gateway is plain HTTP. Token leaks if anyone on
  the LAN packet-captures. Acceptable for trusted home LAN; not acceptable on
  coffee-shop Wi-Fi. If you ever need that, run Tailscale instead of binding
  to the LAN IP, and let Tailscale handle transport security.
- **Windows host:** both services bind to `127.0.0.1` only, no external
  exposure. Anyone with a local user shell on that machine can call the
  endpoints freely (no Bearer in front). If you ever expose the Windows host
  to LAN/WAN, put Caddy+Bearer in front the same way macOS does (see
  `host/windows/README.md` extending section).
- The chatlog daemon (either OS) reads the entire local WeChat history.
  Anyone who reaches `127.0.0.1:5030` on the host can read everything. Don't
  expose port 5030 directly to anything other than localhost.
