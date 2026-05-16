# market-brief

> Windows scheduled task + Claude Code skill that, hourly during US trading
> hours, scans your Discord and WeChat investing chat groups, summarizes them
> into a tier-tagged markdown brief, and pushes it to your WeChat (primary)
> with email as fallback.

## What it does

```
Task Scheduler (08:00–22:00 EDT, hourly)
        ↓
run.ps1
        ↓
claude --print  (prompt.md → uses mcp__chatlog + mcp__discord-selfbot tools)
        ↓
Reports\YYYY-MM-DD-HH-brief.md
        ↓
push_weixin.py  → Hermes Agent → iLink → your WeChat (3–5 chunks)
        ↓ (if WeChat push fails)
Send-MailMessage → Gmail SMTP → your inbox
```

- **Tier-aware**: pre-market (24h window) / market hours (90min) / after-hours
  (90min) — same `prompt.md` switches structure based on EDT hour
- **Markdown chunking**: handled inside Hermes Agent (`_split_text_for_weixin_delivery`,
  2000-char limit, preserves code blocks / tables / headings)
- **Fail-soft to email**: WeChat is the primary channel; email is only sent
  when push fails. Operationally, if you start getting hourly fallback emails
  you know your iLink session needs refresh.

## Install

### As a Claude Code skill (Claude drives the install)

On a Windows machine where you've already run [chat-mcp-setup](https://github.com/ouyadi/mcp-chat-skills/tree/main/skills/chat-mcp-setup)
(provides the `chatlog` + `discord-selfbot` MCP servers):

```powershell
# 1. Clone this repo
git clone https://github.com/ouyadi/market-brief.git $env:USERPROFILE\market-brief

# 2. Make Claude Code discover it as a skill
New-Item -ItemType Junction `
    -Path $env:USERPROFILE\.claude\skills\market-brief-setup `
    -Target $env:USERPROFILE\market-brief

# 3. Restart Claude Code, open a fresh session, say:
#       "use the market-brief-setup skill to install the hourly chat-intel pipeline"
#
# Claude reads SKILL.md and walks through the 9 install steps.
# A few will need you (filling in your groups, scanning a QR with your phone,
# pasting an OAuth token + Gmail App Password). The rest is automated.
```

### Manually (no Claude in the driver's seat)

Read [SETUP-GUIDE.md](SETUP-GUIDE.md) — same content as the skill flow,
written linearly for a human operator.

## Prerequisites

- **Windows 10/11** desktop that stays on during the target hours (Task
  Scheduler can't fire while machine is off)
- **chatlog + discord-selfbot MCP servers** running on `127.0.0.1:5030` /
  `127.0.0.1:6280` — install via [ouyadi/mcp-chat-skills](https://github.com/ouyadi/mcp-chat-skills)
  (the host-side scripts are in that repo's `chat-mcp-setup` skill;
  the `chat-mcp-setup` skill itself is public on GitHub at that link)
- **WeChat 4.x** signed into the account you want scanned on the same machine
- **Claude Code** CLI (`npm install -g @anthropic-ai/claude-code`) +
  long-lived OAuth token from `claude setup-token`
- **Gmail App Password** (Outlook/Hotmail won't work — Microsoft disabled
  basic auth)
- **(Optional, for WeChat push)** ability to scan a QR with your phone's
  WeChat app

## What gets installed on the target machine

| Path | Purpose |
|---|---|
| `C:\Users\<u>\Scripts\market-brief\` | The launcher dir (run.ps1, prompt.md, push_weixin.py, etc.) |
| `C:\Users\<u>\hermes-agent\.venv\` | Python 3.11 venv with Hermes Agent (only if WeChat push enabled) |
| `C:\Users\<u>\.hermes\.env` | iLink credentials from QR scan |
| `C:\Users\<u>\Reports\YYYY-MM-DD-HH-brief.md` | Hourly report output |
| Scheduled Task `MarketBrief` | Fires hourly 08:00–22:00 local time |

## Repo contents

| File | Role |
|---|---|
| [`SKILL.md`](SKILL.md) | Procedural instructions for Claude Code |
| [`SETUP-GUIDE.md`](SETUP-GUIDE.md) | Long-form human-facing install doc |
| [`prompt.template.md`](prompt.template.md) | Scanning prompt template — fill in your groups |
| [`run.ps1`](run.ps1) | PowerShell launcher (claude → WeChat push → email fallback) |
| [`push_weixin.py`](push_weixin.py) | One-shot WeChat sender via Hermes' `send_weixin_direct` |
| [`qr_login_bootstrap.py`](qr_login_bootstrap.py) | One-time iLink QR-scan binding |
| [`schedule-install.ps1`](schedule-install.ps1) | Registers the `MarketBrief` Task Scheduler entry |
| [`secrets.example.json`](secrets.example.json) | Template for `secrets.json` (OAuth + Gmail) |
| [`hermes-py.ps1`](hermes-py.ps1) | Legacy wrapper kept for emergency fallback |

After install on your machine, `secrets.json` and your personalized `prompt.md`
(with your actual group IDs) will exist locally but are gitignored — never
commit those.

## Variants

- **Email-only mode**: if you don't want WeChat push, the skill auto-skips
  Steps 4–6 (Hermes Agent install + QR scan + push smoke test). The pipeline
  degrades cleanly to `claude → email`.
- **macOS**: architecture is identical. The Python helpers (`push_weixin.py`,
  `qr_login_bootstrap.py`) work as-is. Replace `run.ps1` with a shell-script
  equivalent and `Task Scheduler` with `launchd`/`cron`. The MCP server
  setup for macOS is also in [ouyadi/mcp-chat-skills](https://github.com/ouyadi/mcp-chat-skills).
- **Linux**: untested; Hermes Agent supports Linux first-class so should work
  with a `systemd` timer in place of Task Scheduler.

## License

[MIT](LICENSE) — fork, modify, redistribute. Attribution appreciated but not
required.

## Notes / known gotchas

A condensed troubleshooting table is at the bottom of [SKILL.md](SKILL.md#troubleshooting).
The big ones:

- **Don't use uv-managed Python on Windows for Hermes Agent** — Defender has
  been observed quarantining the astral-sh Python distribution. Use
  `winget install Python.Python.3.11` instead. SKILL.md / SETUP-GUIDE.md
  spell this out.
- **Pillow is required but not pulled by `hermes-agent`** — add it explicitly
  in the venv or the QR step crashes.
- **iLink has a ~10 message per session quota** — if hourly fallback emails
  start arriving where you previously got only WeChat messages, send any text
  to the bot in WeChat to refresh, or re-run `qr_login_bootstrap.py` for a
  fresh token. Details in SKILL.md.
