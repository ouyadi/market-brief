# chatlog on Windows

Unlike the macOS daemon (which is fully automated by `install-daemon.sh`),
Windows chatlog setup has **one interactive step** that has to happen in a
real terminal -- extracting the SQLite `data_key` from a running WeChat
process. See the parent [`../README.md`](../README.md) for the full
step-by-step.

## Why no pre-baked `chatlog.json` in this repo

The `data_key` in `host/macos/chatlog/chatlog.json` is a SHA-256 derived from
the macOS WeChat install on Weidis-MacBook-Pro and is **not portable** -- it
won't decrypt Windows WeChat's database. Each Windows host has to extract its
own `data_key` via the TUI.

After extraction, you can commit your `~/.chatlog/chatlog.json` here as a
backup (private repo) -- but it only helps you recreate state on the *same*
Windows machine.

## Source binary

`chatlog-windows-amd64.exe` from [`teest114514/chatlog_alpha`](https://github.com/teest114514/chatlog_alpha)
latest release. Active fork of the deleted `sjzar/chatlog`; supports WeChat 4.x
on Windows. ~35MB.

## Compatibility checks

The fork's README claims auto key-extraction works for:
- Windows WeChat < 4.0.3.36 (older auto-extract path)
- WeChat 4.x via a newer scan technique that works on the alpha builds tested

Beyond those versions you may need to feed `data_key` manually. If the TUI
"Get Data Key" step fails on a too-new WeChat, the workaround is documented
upstream in the chatlog_alpha issues / FAQ.

## File layout once running

```
%USERPROFILE%\chatlog\
├── chatlog.exe              (the binary you downloaded)
└── logs\                    (optional, depending on chatlog version)

%USERPROFILE%\.chatlog\
└── chatlog.json             (account list + data_key, written by TUI)
```

The TUI's "work_dir" defaults to `chatlog\<account>` *relative to cwd* on
Windows -- if you launch `chatlog.exe` from anywhere other than its install
dir, decryption artifacts get scattered. The
`ChatlogServer` task always sets `WorkingDirectory` correctly.

## Daemon-izing without losing the TUI

chatlog has no `--headless` / `serve` CLI mode in the alpha; the TUI process
*is* the server. The Task Scheduler approach
([`../scripts/install-mcp-services.ps1`](../scripts/install-mcp-services.ps1))
launches the TUI minimized so it stays alive but doesn't pop up.

Alternative if minimized-window is too fragile (e.g. you keep accidentally
closing it from the taskbar): wrap chatlog in [NSSM](https://nssm.cc/) or
[WinSW](https://github.com/winsw/winsw) and run as a true Windows service.
Note that running as a Windows service usually means SYSTEM/LocalService
context, which **cannot read the user's OneDrive-synced WeChat data** -- so
configure the service to run as the actual user account (`ObjectName` /
`LocalAccount`).
