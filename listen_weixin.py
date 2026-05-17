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
import re
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
你能调用这些工具:
  - mcp__chatlog__wx_history / wx_search / wx_sessions   微信群历史 / 模糊找群 / 列所有会话
  - mcp__discord-selfbot__read_channel_messages          Discord 频道历史
  - mcp__discord-selfbot__list_channels                  Discord 频道列表
  - mcp__twitter__fetch_tweet_by_url(url)                抓单条 X tweet 内容(用户登录态,绕过登录墙)
  - mcp__twitter__fetch_user_tweets(username, limit)     抓 @某用户最近 N 条 (大 V 跟踪)
  - mcp__twitter__search_tweets(query, limit, mode)      X 关键词搜 (mode='live' 最新 / 'top' 热门)
  - mcp__stock-price__get_quote(ticker)                  实时价 / 涨跌 / 成交量 / 市值 / 52w high-low
  - mcp__stock-price__get_history(ticker, period, interval)  OHLCV 时序(period 1d/5d/1mo/3mo/1y; interval 1m/5m/1h/1d)
  - mcp__stock-price__get_info(ticker)                   sector / forward_pe / next_earnings_date / dividend / 业务概览
  - mcp__stock-price__check_post_hoc(ticker, at_time, horizon)  事后验证:某 ISO 时间点(如 tweet 发布时)+ horizon (1h/1d/3d/1w/2w/1mo),返回 price_at_time / max_gain / max_drawdown / net_move 用于评 KOL/群主 call 命中率
  - WebFetch / WebSearch                                  通用网页(非 X)
  - mcp__chatlog__current_time                            当前美东时间
  - Read / Edit / Write                                   读写本机文件(包括 prompt.md 配置)

**X 工具只读约束**:`mcp__twitter__*` 只暴露读取功能;不要 call 任何 send/like/retweet/follow 之类的(它们没暴露给你,但你也别尝试)。原因:用户主账号 cookies,被 X 反爬抓到 write 行为容易封号。

风格:
  - 不要走 market-brief 的大模板。这是 ad-hoc 问答,不是定时简报。
  - 直接回答用户的问题,带具体数字/出处/ticker。
  - 不发推、不下单。
  - 输出尽量短(<800 字),iLink 单条 ~2000 字会被切片。

# 配置管理模式(关键)

当用户的请求落到这些意图(关键词:**加进监控 / 加到列表 / 删群 / 移除 / 不要监控 / 更新监控群列表 / 把 X 加到简报 / 大 V 加 / 大 V 删 / 大 V 列表 / 跟踪 X 用户**)时,**直接动手改文件,不要问澄清**:

监控配置文件: `C:\\Users\\ouyad\\Scripts\\market-brief\\prompt.md`
  - 微信群在 `### 微信群` 节,表格行格式: `| 群名 | chatroom_id |`
  - Discord 频道在 `### Discord 频道` 节,行格式: `| 服务器 | 频道 | channel_id |`
  - **大 V X 账号在 `### 大 V X 账号` 节**,行格式: `| 大 V 显示名 | X handle (without @) | 主战场 |`

操作流程:
  1. **找 ID** (微信/Discord):
     - 微信群: 调 `mcp__chatlog__wx_sessions` 拉全表,然后按用户给的关键词做 substring 匹配。匹配到唯一群 → 直接用它的 chatroom_id。匹配到多个 → 把候选列出让用户选(只这种情况才问澄清)。
     - Discord: 调 `mcp__discord-selfbot__list_channels` 同理。
  2. **找 handle** (大 V): 用户给的就是 X handle(可能带或不带 `@`,strip 掉)。可选用 `mcp__twitter__fetch_user_tweets(username=...)` 试一下确认 handle 存在 + 顺便记录"主战场"一句话描述。
  3. **改文件**: Read `prompt.md` 找到对应表格末尾,Edit 插入新行(保持表格对齐)。
  4. **删行**: Edit 把整行替换成空字符串(精确匹配 group/handle 关键字)。
  5. **报告**: 改完一句话总结,例如 "已加 3 群..." 或 "已加 2 个大 V: cathiedwood, jimcramer"。**不要打印 prompt.md 全文**。

不要在配置管理模式里问"加到哪个环节/抓取口径"之类。用户说"加进监控"就是加到 prompt.md 的群表格,"大 V 加 XXX" 就是加到大 V 表。

# 一般问答

不属于配置管理的所有其他问题:直接答。如果群名/数据真不清楚,**先用 wx_sessions 或 wx_search 找一下**,而不是问用户澄清。只有当工具也找不到时才让用户提供更多上下文。

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


async def _handle_ping(text: str = "") -> str:
    return f"pong (listener up {_uptime()})"


async def _handle_brief(text: str = "") -> str:
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


async def _handle_help(text: str = "") -> str:
    return (
        "可用命令:\n"
        "  /ping              测试 listener 在线\n"
        "  /brief             立刻触发一次 market-brief\n"
        "  /dv [handle] [Xh]  大 V 速读 (默认: 全部大 V × 过去 2h)\n"
        "                     例: `/dv 6h` / `/dv cathiedwood` / `/dv cathiedwood 24h`\n"
        "  /xfeed [tab] [N]   X 个人时间线简报 (默认: For You + Following, 各 15 条)\n"
        "                     例: `/xfeed` / `/xfeed for_you` / `/xfeed following 25`\n"
        "  /plan [tickers]    可执行方案(stock-price + X live news 富化)\n"
        "                     例: `/plan` (从最新 brief 选 top 3) / `/plan TSLA NVDA INTC`\n"
        "  /help              显示此列表\n"
        "其他任何文本会丢给 Claude 自由问答(中文,带 MCP 工具)。"
    )


async def _handle_dv(text: str = "") -> str:
    """
    Ad-hoc 大 V 速读. Args parsed from text after '/dv':
      no args             → all 大 V in prompt.md, past 2h
      <handle>            → only that handle, past 24h
      <Nh>                → all 大 V, past Nh
      <handle> <Nh>       → combo
    """
    args = text.split()[1:]  # drop '/dv'
    handle: str | None = None
    window: str | None = None
    for a in args:
        if re.match(r"^\d+h$", a, re.I):
            window = a.lower()
        else:
            handle = a.lstrip("@")

    if handle and not window:
        window = "24h"
    if not handle and not window:
        window = "2h"

    if handle:
        prompt = (
            f"用 mcp__twitter__fetch_user_tweets username='{handle}' limit=20 抓最近 tweets。"
            f"过滤出过去 {window} 内的(`posted_at` 跟当前 EDT 比对,用 mcp__chatlog__current_time 拿当前时间)。"
            f"输出简洁中文 markdown:\n"
            f"## @{handle} 最近 {window}\n"
            f"- 每条:`时间(HH:MM)`原文(中文翻译括号补)— **一句话解读**(提到的 ticker / 立场)\n"
            f"末尾一句话 overall 倾向(看多/看空/中性)+ 1-3 个最值得关注的 ticker。\n"
            f"如果窗口内 0 条新推:回复'`@{handle}` 过去 {window} 无新推'。"
        )
    else:
        prompt = (
            f"步骤 1: Read 文件 `C:\\Users\\ouyad\\Scripts\\market-brief\\prompt.md`,定位 '### 大 V X 账号' 节,"
            f"提取该表格里所有 X handle (列 'X handle (without @)')。"
            f"步骤 2: 调 `mcp__chatlog__current_time` 拿当前 EDT,计算 since = now - {window}。"
            f"步骤 3: 对每个 handle 并行调 `mcp__twitter__fetch_user_tweets(username=handle, limit=15)`。"
            f"步骤 4: 过滤掉 posted_at < since 的推。**0 条新推的 handle 在最终输出里跳过**。"
            f"步骤 5: 输出简洁中文 markdown:\n"
            f"## 🎙️ 大 V 速读 — 过去 {window}\n"
            f"按信息量 ×独家性排序,每个 handle 一小段:\n"
            f"- **@handle** ({{N}} 条新推):\n"
            f"  - `时间(HH:MM)` 原文(<200 字,长则截断) — 一句话解读(提到的 ticker / 立场)\n"
            f"末尾跨大 V 信号(可选):\n"
            f"> ⚡ 跨大 V:\n"
            f"> - 共识看多/看空: 若 ≥2 大 V 提同 ticker 方向一致\n"
            f"> - 新出现 ticker: 该窗口首次被任一大 V 提及的\n"
            f"如果所有大 V 都 0 条:回复 '过去 {window} 跟踪的大 V 全员无新推'。\n"
            f"如果 mcp__twitter__ 工具不可用:回复'X-MCP 暂不可用(检查 TwitterMCP scheduled task)'。\n"
            f"**整体输出 <2000 字**(iLink 单条上限)。"
        )

    return await _ask_claude(prompt)


async def _handle_xfeed(text: str = "") -> str:
    """
    Ad-hoc brief over the user's X home timeline. Args after '/xfeed':
      no args              both For You + Following, 15 tweets each
      for_you / fy         only For You
      following / fl       only Following
      <N>                  both with N tweets each (capped 30)
    """
    args = text.split()[1:]
    tab_filter: str | None = None
    limit = 15
    for a in args:
        a_low = a.lower()
        if a_low in ("for_you", "foryou", "fy"):
            tab_filter = "for_you"
        elif a_low in ("following", "fl", "follow"):
            tab_filter = "following"
        elif a_low.isdigit():
            limit = max(1, min(30, int(a_low)))

    if tab_filter:
        title = "🌐 For You" if tab_filter == "for_you" else "👥 Following"
        prompt = (
            f"调 `mcp__twitter__fetch_home_timeline tab='{tab_filter}' limit={limit}`。\n"
            f"输出中文 markdown 简报:\n"
            f"## 📡 {title} 简报(最新 {limit} 条)\n\n"
            f"按信息量排序,**过滤掉**段子/广告/纯个人生活。每条:\n"
            f"- **@handle** `HH:MM`: 原文(<150 字)— 一句话解读(ticker / 方向 / 事件)\n\n"
            f"末尾:**🎯 关键词/Ticker 频次** 3-5 个出现 ≥2 次的 ticker 或主题。\n"
            f"输出 < 1800 字。"
        )
    else:
        prompt = (
            f"并行调用两个工具:\n"
            f"  1. mcp__twitter__fetch_home_timeline tab='for_you' limit={limit}\n"
            f"  2. mcp__twitter__fetch_home_timeline tab='following' limit={limit}\n\n"
            f"输出中文 markdown:\n"
            f"## 📡 X 个人时间线简报\n\n"
            f"### 🌐 For You (X 算法推)\n"
            f"5-10 条最有信息量,过滤段子/广告。每条:\n"
            f"- **@handle** `HH:MM`: 原文 — 一句话解读\n\n"
            f"### 👥 Following (我关注的人)\n"
            f"同上格式。\n\n"
            f"### 🎯 共同信号\n"
            f"两个 tab 都出现 ≥2 次的 ticker / 主题 / 关键词。如果零交集,标'无明显共同信号'。\n\n"
            f"全报告 < 1900 字(iLink 单条上限)。**严格过滤**:段子、广告、纯个人生活全 skip,只留:股票/宏观/科技产品/政策。"
        )
    return await _ask_claude(prompt)


async def _handle_plan(text: str = "") -> str:
    """
    Ad-hoc enriched execution plan.
      /plan                    — Claude reads latest brief, picks top 3 tickers
      /plan TSLA               — single ticker
      /plan TSLA NVDA INTC     — up to 5 tickers
    """
    args = text.split()[1:]
    tickers = []
    for a in args:
        clean = a.upper().lstrip("$").strip(",")
        if re.match(r"^[A-Z]{1,5}$", clean):
            tickers.append(clean)
    tickers = tickers[:5]

    if not tickers:
        intro = (
            "步骤 0:Read `C:\\Users\\ouyad\\Reports\\` 目录下**最新**一份 "
            "`YYYY-MM-DD-HH-brief.md`(按文件名排序取最大)。从其 "
            "'## ⚡ 高优先级关注' / '## 🎯 个股共识' 节里提取 **top 3** tickers "
            "(信号强度 × 时效优先)。如无法判断,默认取 brief 头部"
            "出现频次最高的 3 个。\n\n"
        )
    else:
        intro = f"对以下 tickers 富化:**{', '.join(tickers)}**。\n\n"

    body = (
        "**对每个 ticker 并行调** 4 个工具:\n"
        "  1. `mcp__stock-price__get_quote(ticker)`\n"
        "  2. `mcp__stock-price__get_info(ticker)`\n"
        "  3. `mcp__stock-price__get_history(ticker, period='5d', interval='1h')`\n"
        "  4. `mcp__twitter__search_tweets(query='$'+ticker, limit=8, mode='live')`\n\n"
        "**输出中文 markdown,每个 ticker 一节**:\n\n"
        "### {TICKER} ─ {一句话定位 / 多空 ±conviction}\n"
        "- **Snapshot**:`$price | day low-high | 52w low-high | mc | next earnings: date | fwd P/E | 50d/200d MA`\n"
        "- **消息面** (X live × N):3-5 条 actionable,过滤段子/广告。每条 `HH:MM @user`: 主旨(<80 字)\n"
        "- **技术位** (5d 1h):支撑 `$s1 / $s2`;压力 `$r1 / $r2`;最近趋势一句话\n"
        "- **可执行**:\n"
        "  - 立场:多/空/观望\n"
        "  - Entry:`$price` 或区间\n"
        "  - Stop:`$price`(理由)\n"
        "  - Target:`$price`(R/R)\n"
        "  - 触发:catalyst / break level / 时间窗口\n"
        "  - 失效:stop hit / 反向 catalyst\n"
        "  - 时效:几天 / 本周 / 财报前 / 等政策\n"
        "- **风险/红旗**:一句话\n\n"
        "**约束**:整体输出 < 1800 字(iLink 单条上限)。如某 ticker 的工具失败,标"
        "'数据源缺失:{ticker} - {工具}'但不要 abort 整轮。"
    )
    return await _ask_claude(intro + body)


COMMANDS = {
    "/ping": _handle_ping,
    "/brief": _handle_brief,
    "/dv": _handle_dv,
    "/xfeed": _handle_xfeed,
    "/plan": _handle_plan,
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
        reply = await handler(text)
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
