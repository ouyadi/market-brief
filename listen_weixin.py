"""
listen_weixin.py — inbound iLink long-poller that forwards messages to Claude.

For each incoming message addressed to our bot (filtered to the configured
WEIXIN_HOME_CHANNEL so we only respond to the bot owner, not strangers):

  1. Filter out our own outbound echoes and empty/typing-only messages.
  2. Match special slash commands first (cheap, no Claude spend):
       /ping              -> reply "pong" + uptime
       /brief             -> Start-ScheduledTask MarketBrief (forces a run now)
       /help              -> reply the command list
  3. Anything else: spawn `claude --print --dangerously-skip-permissions`
     with a small system-prompt wrapper that introduces the stock-intel
     persona and the available MCP tools, feed the user's text on stdin,
     capture stdout, and push it back via send_weixin_direct.

Persisted state:
  ~/.hermes/weixin/accounts/<account_id>.sync.<...>  -- get_updates cursor
  (reused via gateway.platforms.weixin._load_sync_buf / _save_sync_buf)

Required env vars (from $HOME/.hermes/.env, written by qr_login_bootstrap.py):
  WEIXIN_ACCOUNT_ID, WEIXIN_TOKEN, WEIXIN_BASE_URL, WEIXIN_HOME_CHANNEL
Plus, for the Claude subprocess:
  CLAUDE_CODE_OAUTH_TOKEN     (loaded from secrets.json by the wrapper .ps1)

Run interactively for testing:
  python listen_weixin.py
Or via the install-listener.ps1 scheduled task at log on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiohttp

from gateway.platforms.weixin import (
    EP_GET_UPDATES,
    _api_post,
    _base_info,
    _extract_text,
    _load_sync_buf,
    _make_ssl_connector,
    _save_sync_buf,
    send_weixin_direct,
)

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
LOG_DIR = Path(os.environ.get("LISTEN_LOG_DIR") or (Path.home() / "Scripts" / "market-brief" / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "listen_weixin.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("listen")

START_TIME = time.monotonic()
GET_UPDATES_TIMEOUT_MS = 30_000
LONG_POLL_RETRY_BACKOFF_S = 5.0
CLAUDE_TIMEOUT_S = 600  # 10 min; long enough for MCP-heavy answers

SYSTEM_PROMPT = """\
你是一名美股情报员,通过微信跟用户对话。回答必须简洁、可操作、中文。
你能调用这些 MCP 工具来现拉数据:
  - mcp__chatlog__wx_history / wx_search / wx_sessions   微信群历史
  - mcp__discord-selfbot__read_channel_messages          Discord 频道
  - WebFetch / WebSearch                                  网页
  - mcp__chatlog__current_time                            当前美东时间

风格:
  - 不要走 morning-brief 的大模板。这是 ad-hoc 问答,不是定时简报。
  - 直接回答用户的问题,带具体数字/出处/ticker。
  - 不发推、不下单。
  - 如果用户的问题不清楚,问一句澄清,不要乱猜。
  - 输出尽量短(<800 字),iLink 单条 ~2000 字会被切片。

用户的提问:
"""


def _parse_env_file(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _creds() -> dict:
    env = _parse_env_file(HERMES_HOME / ".env")
    return {
        "account_id": env.get("WEIXIN_ACCOUNT_ID", "").strip(),
        "token": env.get("WEIXIN_TOKEN", "").strip(),
        "base_url": env.get("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com").strip().rstrip("/"),
        "home_channel": env.get("WEIXIN_HOME_CHANNEL", "").strip(),
    }


# ────────────────────────────────────────────────────────────────────────────
#  command handlers
# ────────────────────────────────────────────────────────────────────────────

def _uptime() -> str:
    s = int(time.monotonic() - START_TIME)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}m{s:02d}s"


async def _handle_ping() -> str:
    return f"pong (listener up {_uptime()})"


async def _handle_brief() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-Command",
            "Start-ScheduledTask -TaskName MarketBrief",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            return "✓ MarketBrief 已触发,下一份简报 3-6 分钟内到。"
        return f"✗ 触发失败 (exit {proc.returncode}): {out.decode('utf-8','replace')[:200]}"
    except Exception as exc:
        return f"✗ 触发异常: {exc}"


async def _handle_help() -> str:
    return (
        "可用命令:\n"
        "  /ping   测试 listener 在线\n"
        "  /brief  立刻触发一次 market-brief\n"
        "  /help   显示此列表\n"
        "其他任何文本会丢给 Claude 自由问答(中文,带 MCP 工具)。"
    )


COMMANDS = {
    "/ping": _handle_ping,
    "/brief": _handle_brief,
    "/help": _handle_help,
}


# ────────────────────────────────────────────────────────────────────────────
#  Claude dispatch
# ────────────────────────────────────────────────────────────────────────────

async def _ask_claude(user_text: str) -> str:
    """Run `claude --print --dangerously-skip-permissions` with the user text.

    `claude` on Windows is `claude.CMD`, which asyncio's CreateProcess can't
    launch directly. Always go through cmd.exe so PATHEXT resolution happens.
    """
    prompt = SYSTEM_PROMPT + user_text.strip()

    proc = await asyncio.create_subprocess_exec(
        os.environ.get("COMSPEC", "cmd.exe"),
        "/c",
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--output-format", "text",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=CLAUDE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return f"❌ Claude 超时 ({CLAUDE_TIMEOUT_S}s 无回应)"

    if proc.returncode != 0:
        err_tail = stderr.decode("utf-8", "replace")[-400:]
        return f"❌ Claude 异常 (exit {proc.returncode}):\n{err_tail}"

    reply = stdout.decode("utf-8", "replace").strip()
    return reply or "(Claude 返回空)"


# ────────────────────────────────────────────────────────────────────────────
#  inbound loop
# ────────────────────────────────────────────────────────────────────────────

async def _push_reply(creds: dict, reply: str) -> None:
    """Push reply text back to the user's chat via Hermes."""
    if not reply.strip():
        return
    extra = {"account_id": creds["account_id"], "base_url": creds["base_url"]}
    result = await send_weixin_direct(
        extra=extra,
        token=creds["token"],
        chat_id=creds["home_channel"],
        message=reply,
    )
    if not result.get("success"):
        log.error("push back failed: %s", result.get("error"))


async def _handle_message(creds: dict, message: dict) -> None:
    sender_id = str(message.get("from_user_id") or "").strip()
    if not sender_id:
        return
    # Ignore our own outbound echoes (defensive — iLink usually doesn't echo
    # but better safe than a feedback loop).
    if sender_id == creds["account_id"]:
        return
    # Only respond to the bot owner. Strangers DMing the bot are ignored.
    if sender_id != creds["home_channel"]:
        log.info("ignoring message from non-owner sender=%s", sender_id[:8])
        return

    text = _extract_text(message.get("item_list") or []).strip()
    if not text:
        return  # typing-only, media-only, etc.

    log.info("inbound: %r", text[:120])

    # Match special commands (case-sensitive, must be prefix)
    cmd_key = text.split()[0].lower()
    handler = COMMANDS.get(cmd_key)
    if handler is not None:
        reply = await handler()
    else:
        reply = await _ask_claude(text)

    await _push_reply(creds, reply)
    log.info("replied: %r", reply[:120])


async def main_loop() -> int:
    creds = _creds()
    missing = [k for k in ("account_id", "token", "home_channel") if not creds[k]]
    if missing:
        log.error("missing creds: %s — run qr_login_bootstrap.py first", missing)
        return 2

    log.info("listener up. account=%s chat=%s",
             creds["account_id"][:8] + "…", creds["home_channel"][:8] + "…")

    sync_buf = _load_sync_buf(str(HERMES_HOME), creds["account_id"]) or ""

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        while True:
            try:
                resp = await _api_post(
                    session,
                    base_url=creds["base_url"],
                    endpoint=EP_GET_UPDATES,
                    payload={"get_updates_buf": sync_buf},
                    token=creds["token"],
                    timeout_ms=GET_UPDATES_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                continue   # normal: long-poll timed out idle, just loop
            except Exception as exc:
                log.warning("get_updates error: %s — backing off %.1fs",
                            exc, LONG_POLL_RETRY_BACKOFF_S)
                await asyncio.sleep(LONG_POLL_RETRY_BACKOFF_S)
                continue

            ret = resp.get("ret", 0)
            if ret and ret != 0:
                log.warning("get_updates ret=%s msg=%s", ret, resp.get("errmsg"))
                await asyncio.sleep(LONG_POLL_RETRY_BACKOFF_S)
                continue

            msgs = resp.get("msgs") or []
            new_buf = resp.get("get_updates_buf") or sync_buf
            if new_buf != sync_buf:
                sync_buf = new_buf
                _save_sync_buf(str(HERMES_HOME), creds["account_id"], sync_buf)

            for m in msgs:
                try:
                    await _handle_message(creds, m)
                except Exception:
                    log.exception("handler crashed on message")


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main_loop()))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)
