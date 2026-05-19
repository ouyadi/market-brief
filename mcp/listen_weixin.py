"""
listen_weixin.py — inbound iLink long-poller that forwards messages to the LLM backend.

For each incoming message addressed to our bot (filtered to the configured
WEIXIN_HOME_CHANNEL so we only respond to the bot owner, not strangers):

  1. Filter out our own outbound echoes and empty/typing-only messages.
  2. Match special slash commands first (cheap, no LLM spend):
       /ping              -> reply "pong" + uptime
       /brief             -> Start-ScheduledTask MarketBrief (forces a run now)
       /help              -> reply the command list
  3. Anything else: spawn the configured LLM CLI (`codex exec` by default,
     or `claude --print` when MARKET_BRIEF_LLM_BACKEND=claude)
     with a small system-prompt wrapper that introduces the stock-intel
     persona and the available MCP tools, feed the user's text on stdin,
     capture stdout, and push it back via send_weixin_direct.

Persisted state:
  ~/.hermes/weixin/accounts/<account_id>.sync.<...>  -- get_updates cursor
  (reused via gateway.platforms.weixin._load_sync_buf / _save_sync_buf)

Required env vars (from $HOME/.hermes/.env, written by qr_login_bootstrap.py):
  WEIXIN_ACCOUNT_ID, WEIXIN_TOKEN, WEIXIN_BASE_URL, WEIXIN_HOME_CHANNEL
LLM backend:
  MARKET_BRIEF_LLM_BACKEND    codex (default) or claude
  CLAUDE_CODE_OAUTH_TOKEN     only needed when backend=claude

Run interactively for testing:
  python listen_weixin.py
Or via the install-listener.ps1 scheduled task at log on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiohttp

# Sibling module: self-evolution JSON schema + proposal storage (Phase 2b/2c/2d).
# Lives next to this file at ~/Scripts/market-brief/selfevolve.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
import selfevolve  # noqa: E402

from gateway.platforms.weixin import (
    EP_GET_UPDATES,
    _api_post,
    _base_info,
    _extract_text,
    _get_config,
    _load_sync_buf,
    _make_ssl_connector,
    _save_sync_buf,
    _send_typing,
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
LLM_TIMEOUT_S = int(os.environ.get("MARKET_BRIEF_LLM_TIMEOUT_S", "900"))  # 15 min ceiling; long enough for MCP-heavy answers like /critique that re-fetch raw chat windows. Most slash commands finish in <2 min; this is the safety net.

# Experimental: periodically ping iLink with a typing indicator (status=1
# then immediately status=0) to test whether the typing endpoint refreshes
# the outbound send-session enough to avoid needing the user to touch the
# bot daily. Set to 0 to disable. Cheap and zero-quota (typing is its own
# endpoint, not sendmessage). Logs are tagged 'keepalive:' so we can
# correlate keepalive activity with successful tokenless pushes later.
KEEPALIVE_INTERVAL_S = int(os.environ.get("KEEPALIVE_INTERVAL_S", "1800"))  # 30 min

# Phase 2b: commands that emit <proposals>JSON</proposals> blocks for the
# self-evolution applier to consume. Other commands (/ping, /brief, /dv, etc.)
# have no JSON block and skip the extraction step in _handle_inbound.
SELF_EVOLVE_COMMANDS = frozenset({"/reflect", "/critique", "/score", "/kol_drift", "/heat"})

# Cross-platform: PROMPT_MD_PATH is the absolute path the configuration-
# management mode will Read/Edit. Defaults to ~/Scripts/market-brief/prompt.md
# on both Windows and macOS. Override with MARKET_BRIEF_DIR env var if
# you install the runtime files elsewhere.
SCRIPTS_DIR = Path(os.environ.get("MARKET_BRIEF_DIR") or (Path.home() / "Scripts" / "market-brief"))
PROMPT_MD_PATH = SCRIPTS_DIR / "prompt.md"
MEMORY_MD_PATH = SCRIPTS_DIR / "memory.md"

SYSTEM_PROMPT = f"""\
你是一名美股情报员,通过微信跟用户对话。回答必须简洁、可操作、中文。
你能调用这些工具:
  - mcp__wxstore__wxstore_history(chat, since='86400', until=None, limit=100)  **微信群消息时间范围查询**(从我们自己的 SQLite 读,与 WeChat 文件无锁竞争)。`since` 支持 '86400'/'5400' 秒数字符串或 '24h'/'7d'/'30d',`chat='all'` = 全部群。返回 JSON 含 messages 数组。**优先用这个,不是老的 wx_history**
  - mcp__wxstore__wxstore_semantic(query, chats='', window='30d', limit=20)  **跨群语义向量检索**(qwen3-embedding 本地)。比关键词搜索召回率高,适合:
      · 跨窗口找话题:"过去 30 天讨论 INTC 加仓的群"
      · 反向找 KOL:"哪些群提过 Tam 通胀 6% 预测"
      · 题材映射:"长鑫存储 IPO" → 召回所有跨群相关讨论(即使没出现 cashtag)
      `chats` 是逗号分隔 chatroom_id(空字符串 = 全部已索引);`window` 用 1d/7d/30d/90d/1y;`limit` 是 top_k
  - mcp__wxstore__wxstore_stats()  看 wxstore 内部状态(总消息数、各群分布、最新消息时间)。诊断 ingest 是否正常工作
  - mcp__chatlog__wx_sessions / wx_search / wx_chatrooms   会话列表 / 关键词 / 群列表(`wx_history` 已弃用 — 改用 wxstore_history!chatlog 跟 WeChat 抢文件锁会挂)
  - mcp__discord-selfbot__read_channel_messages          Discord 频道历史
  - mcp__discord-selfbot__list_channels                  Discord 频道列表
  - mcp__twitter__fetch_tweet_by_url(url)                抓单条 X tweet 内容(用户登录态,绕过登录墙)。**默认 include_thread=True,自动展开同作者 self-reply 形成的 thread**;返回的 dict 里若有 `thread` 字段说明这条是 thread head
  - mcp__twitter__fetch_thread(url, max_tweets=20)       专门抓 thread 的工具。当你看到 "Show this thread" / 推文以 `1/N`、`(续)`、编号列表 (一、二、三) 开头、或文末像被截断时,**主动**调这个。返回 focal + 后续 self-reply 的有序列表
  - mcp__twitter__fetch_user_tweets(username, limit)     抓 @某用户最近 N 条 (大 V 跟踪)。**注意**:这个工具拿到的可能只是 thread head,如果有 thread 标志,follow up 调 fetch_thread
  - mcp__twitter__search_tweets(query, limit, mode)      X 关键词搜 (mode='live' 最新 / 'top' 热门)
  - mcp__stock-price__get_quote(ticker)                  实时价 / 涨跌 / 成交量 / 市值 / 52w high-low
  - mcp__stock-price__get_history(ticker, period, interval)  OHLCV 时序(period 1d/5d/1mo/3mo/1y; interval 1m/5m/1h/1d)
  - mcp__stock-price__get_info(ticker)                   sector / forward_pe / next_earnings_date / dividend / 业务概览
  - mcp__stock-price__check_post_hoc(ticker, at_time, horizon)  事后验证:某 ISO 时间点(如 tweet 发布时)+ horizon (1h/1d/3d/1w/2w/1mo),返回 price_at_time / max_gain / max_drawdown / net_move 用于评 KOL/群主 call 命中率
  - mcp__stock-price__list_expirations(ticker)           所有可交易期权到期日(用于找下一个最近 expiry)
  - mcp__stock-price__get_option_chain(ticker, expiration, contract_type, near_strike_pct, limit, with_greeks)  期权链。默认 ±10% near spot 范围 + 自动算 Δ/Γ/Θ/Vega/Rho。contract_type 可选 calls/puts/both
  - mcp__stock-price__implied_move(ticker, expiration)   ATM straddle / spot = 期权市场定的 ±X% 波动范围 + 上下 breakeven。**财报 setup 核心指标**
  - mcp__stock-price__unusual_activity(ticker, expiration, min_vol_oi_ratio, min_volume)  volume >> open interest 的 strikes,用于交叉验证群里说的"$XXX call 大单"
  - mcp__stock-price__compute_greeks(ticker, expiration, strike, contract_type)  单 strike 希腊字母(ad-hoc 查询)
  - mcp__polymarket__list_markets(query, limit, active_only, sort_by)  预测市场列表(Polymarket Gamma API)。每条返回 outcomePrices = 市场隐含概率(0.0-1.0)。query 模糊匹配 question;sort_by 可选 volume24hr/volume1wk/volume1mo/liquidity/endDate
  - mcp__polymarket__get_market(id_or_slug)              单个市场详情
  - mcp__polymarket__list_events(query, limit, active_only)  事件列表。事件 = 一组相关市场(比如 "Fed June 会议" 下挂 25bp/50bp/hold 三个子市场)
  - mcp__polymarket__get_event(id_or_slug)               单个事件 + 全部子市场
  - mcp__polymarket__top_movers(window='1mo', limit)     最近 1 月概率变动最大的市场(Gamma 只暴露 oneMonthPriceChange)
  - mcp__polymarket__get_price_history(id_or_slug, lookback_hours, fidelity_minutes)  单个市场概率时间序列(走 CLOB,可自定义采样)。用来画图或丢给 LLM 自己算
  - mcp__polymarket__prob_change(id_or_slug, lookback)   任意窗口的概率变化:lookback 用 '15m' / '1h' / '6h' / '24h' / '7d' / '1w' / '1mo' 等。**比 Gamma 的 oneMonthPriceChange 灵活,这是日常用最多的工具**
  - mcp__polymarket__compute_vol(id_or_slug, lookback_days)  概率 log-return 的年化实现波动率。注意不是 BS-IV(Polymarket 不是期权),解读为"市场分歧度":高 = 还在博弈,低 = 共识(或薄流动性)
  - mcp__polymarket__short_movers(window_hours, limit, scan_size)  真正的短窗口 movers (1h/24h/7d 都行)。代价:扫 scan_size 个市场各拉 history,5-15s/次,别频繁调
  - mcp__financialjuice__list_headlines(since, limit, query, tag)   FinancialJuice 实时财经/地缘 wire(本地 5min poll cache)。since '15m'/'1h'/'24h' 等;tag 可选 fed/macro/trump/geopolitics/earnings/crypto/ipo/china
  - mcp__financialjuice__get_tagged(tag, limit, since)              按 tag 速取(同 list_headlines tag= 但更简短)
  - mcp__financialjuice__get_for_ticker(ticker, since, limit)       找含 $TICKER 提及的 headline(注:wire 经常用公司名而不是 cashtag,可能少)
  - mcp__financialjuice__cache_status()                              cache 健康 + tag 分布
  - WebFetch / WebSearch                                  通用网页(非 X)
  - mcp__chatlog__current_time                            当前美东时间
  - Read / Edit / Write                                   读写本机文件(包括 prompt.md 配置)

**X 工具只读约束**:`mcp__twitter__*` 只暴露读取功能;不要 call 任何 send/like/retweet/follow 之类的(它们没暴露给你,但你也别尝试)。原因:用户主账号 cookies,被 X 反爬抓到 write 行为容易封号。

**图片推处理**:fetch_* 返回的每条推都带 `media` 字段(list)。若 `text==""` 但 `media` 非空,说明这是图片/视频推 (比如郭明錤、何同学这种把分析做成长图发的 KOL)。处理方式:
  - photo: 对每个 `media[i].url` 调 `WebFetch(url=...)` 让我读图(我自带 vision,会直接 OCR/描述)。多张图就多次调
  - video: 通常拿不到原视频,有 `poster` 字段就 fetch poster 描述封面;没的话告诉用户"视频推,无法解析"
  - 若同时有 text 和 media,优先 text;media 作为补充信息可选择性 fetch

风格:
  - 不要走 market-brief 的大模板。这是 ad-hoc 问答,不是定时简报。
  - 直接回答用户的问题,带具体数字/出处/ticker。
  - 不发推、不下单。
  - 输出尽量短(<800 字),iLink 单条 ~2000 字会被切片。

**重要 — 跨消息上下文**:每条微信消息都是 fresh LLM session,你**看不到**上一条消息或之前 slash command 的回复。所以**当用户提到"上次"/"刚才"/"按你建议"/"那几只"等指代**,你**必须**:
  1. **Read 本机文件** `{SCRIPTS_DIR}/last_<cmd>.md`(例如 `last_heat.md`、`last_critique.md`、`last_dv.md`、`last_plan.md`、`last_ticker.md`、`last_kol_drift.md`、`last_score.md`)拿上次该 slash command 的完整输出
  2. **Read 持久记忆** `{MEMORY_MD_PATH}` 拿用户跨次对话稳定的偏好(信号金字塔 / 反指 KOL / SKM = Anthropic proxy 这类)
  3. **Read 配置** `{PROMPT_MD_PATH}` 看当前 watchlist / KOL 表

**这三个文件确实存在**,不要轻信"找不到"就回"我看不到上下文" — 一定先 Read 试一下。Read 失败再说找不到。

**重要 — 简报细节查询路径(关键)**:用户在微信收到的是**只有"📱 微信速读"的精简版**(≤1900 字, 5 个 setup + 跨V + 宏观 + 技术位)。当用户在微信对话里**进一步问简报细节**, 关键词例:**"X 详情" / "刚才 INTC 那段展开" / "ASTS JPM 那段" / "完整 INTC 分析" / "把 setup 都给我" / "🎙 大 V 都说啥" / "宏观展开"** 等, 你**必须**:

  1. **不重跑分析** — **绝不**调 `mcp__twitter__*` / `mcp__chatlog__*` / `mcp__polymarket__*` / `mcp__financialjuice__*` 等去重新拉数据. 那要 5-10 分钟而且会和定时简报抢资源
  2. **Read 最新简报存档** — 调 `mcp__chatlog__current_time` 拿当前 EDT, 算出最新简报路径(格式 `~/Reports/{{YYYY-MM-DD}}-{{HH 两位}}-brief.md`, 跨平台用 `Path.home()`). 如果当前小时简报还没写出,fallback 到上一小时
  3. **按用户问的范围 grep / extract**:
     - 问某 ticker(如 "INTC 详情"): 用 Grep 或者 Read 整份, 找所有 INTC 相关的 ⚡/🎯/🎙/🏛 sub-section, 拼接成一条 ≤1900 字回复
     - 问某 section(如 "🎙 都说啥"): extract 整个 ## 🎙 大 V 速读 section 内容
     - 问"完整 setup": extract ## ⚡ 高优先级关注 整段
  4. **回复格式**: 不带 markdown bold, 不带 doc header(用户已读速读了), 直接把 grep 出来的内容**精简后**推回, 控制 ≤1900 字 (超了告诉用户 "完整本机在 `~/Reports/...md`, 用 PC 看")
  5. **如果简报文件不存在**(如系统刚开机): 告诉用户 "本时段简报还没生成, 下次 brief 时间是 HH:00 EDT", 不要硬编造内容

# 配置管理模式(关键)

当用户的请求落到这些意图(关键词:**加进监控 / 加到列表 / 删群 / 移除 / 不要监控 / 更新监控群列表 / 把 X 加到简报 / 大 V 加 / 大 V 删 / 大 V 列表 / 跟踪 X 用户 / 个股加 / 个股删 / 个股列表 / 加追踪 / 跟踪个股 / watchlist**)时,**直接动手改文件,不要问澄清**:

监控配置文件: `{PROMPT_MD_PATH}` (表结构 / scan 范围 / 时段感知 / 输出模板等)
持续记忆文件: `{MEMORY_MD_PATH}` (跨次简报的用户角度 / 反指标记 / thesis 纠正等)。**两个文件配对使用**:配置变更通常都改 prompt.md;**用户提供的真实角度 / 反指 / "X 是 Y 的 proxy" 这类 thesis 纠正,要同步写进 memory.md** 的"KOL 真实角度" / "Watchlist 真实跟踪角度" 表(append 一行,或更新已有行),这样下次定时简报会自动加载
  - 微信群在 `### 微信群` 节,表格行格式: `| 群名 | chatroom_id |`
  - Discord 频道在 `### Discord 频道` 节,行格式: `| 服务器 | 频道 | channel_id |`
  - **大 V X 账号在 `### 大 V X 账号` 节**,行格式: `| 大 V 显示名 | X handle (without @) | 主战场 |`
  - **个股 watchlist 在 `### 个股 watchlist` 节**,行格式: `| Ticker | 关注理由 |` (ticker 大写,不带 `$`)

操作流程:
  1. **找 ID** (微信/Discord):
     - 微信群: 调 `mcp__chatlog__wx_sessions` 拉全表,然后按用户给的关键词做 substring 匹配。匹配到唯一群 → 直接用它的 chatroom_id。匹配到多个 → 把候选列出让用户选(只这种情况才问澄清)。
     - Discord: 调 `mcp__discord-selfbot__list_channels` 同理。
  2. **找 handle** (大 V): 用户给的就是 X handle(可能带或不带 `@`,strip 掉)。可选用 `mcp__twitter__fetch_user_tweets(username=...)` **仅**验证 handle 存在。**不要自己编"主战场"描述** —— 用户没明说时填 `<待用户补充>`,只填用户的原话或字面 sector 标签。**绝不**根据训练数据/general knowledge 脑补 "X 是 Y 的 proxy" / "Z 是反指" 之类的理由 —— 这种没经验证的 attribution 后期会污染简报的判断逻辑。
     **找 ticker** (个股): 大写 1-5 字母 ASCII (strip 掉前导 `$`)。用户加 ticker 时若**显式带了理由**就用用户的原话;若**没给理由**,填 `<待用户补充>`,**不要**根据训练数据写"半导体大票/AI 转型"这类听起来对的通用描述 —— 用户的真实 thesis 经常跟 generic 描述无关(例:SKM 不是韩国电信防御票,是 Anthropic equity proxy)。可以**只**填一行 factual sector + mc 作为占位 (例:"<待用户补充 — sector: tech, mc: $5.4T>"),但**不要**编投资角度。
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
    # Pick the right scheduler kick command per platform.
    # Windows: Task Scheduler  -> powershell Start-ScheduledTask MarketBrief
    # macOS:   launchd         -> launchctl kickstart -k gui/<uid>/com.ouyadi.market-brief
    if sys.platform == "win32":
        cmd = (
            "powershell.exe", "-NoProfile", "-Command",
            "Start-ScheduledTask -TaskName MarketBrief",
        )
    elif sys.platform == "darwin":
        cmd = (
            "launchctl", "kickstart", "-k",
            f"gui/{os.getuid()}/com.ouyadi.market-brief",
        )
    else:
        return f"✗ /brief 不支持当前平台: {sys.platform}"

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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
        "  /ticker TICKER     单 ticker 的 X 讨论速读 (cashtag search)\n"
        "                     例: `/ticker SKM` / `/ticker INTC 6h` / `/ticker NVDA 30 top`\n"
        "  /critique [N]      meta-审查最近简报漏了哪些 gap(stratified-random 抽 3 Discord + 3 微信,3-5 min)\n"
        "                     每次采样不同,多跑几次覆盖全集\n"
        "                     例: `/critique` (最新) / `/critique 2` (n 份前)\n"
        "  /kol_drift         (A) 检测大 V 主战场漂移,>30% 偏离时建议更新描述\n"
        "  /heat [N]          (B) 从最近 N 份简报抽未在 watchlist 的高频 ticker(默认 4 份)\n"
        "                     例: `/heat` / `/heat 8` / `/heat 24`\n"
        "  /score [Nd] [hzn]  (C) 过去 N 天盘前简报 ⚡ section 推荐的命中率\n"
        "                     例: `/score` (7d, horizon 3d) / `/score 14d 1w`\n"
        "  /reflect [Nd]      自我检讨 ≥2 次重复出现的运行经验,提议沉淀到 memory.md (正向/负向)\n"
        "                     例: `/reflect` (7d) / `/reflect 14d`\n"
        "  /search [Nd] q     跨群语义检索(wxstore 主存储)\n"
        "                     例: `/search INTC 加仓` (30d) / `/search 7d 通胀 6%`\n"
        "\n"
        "  ── 闭环 applier (Phase 2c+2d,人审 self-evolve 提议) ──\n"
        "  /proposals [Nd]    列出待审 proposals (近 30d 默认)\n"
        "  /show <id>         显示某个 proposal 的完整内容 + diff preview\n"
        "  /apply <id>        落盘(只对 writable=✓ 的;BLACKLISTED 会拒)\n"
        "  /reject <id>       丢弃(移到 rejected/)\n"
        "  /rollback <file>   `memory.md` 或 `prompt.md` 回退到最近一次 apply 之前\n"
        "\n"
        "  /help              显示此列表\n"
        "其他任何文本会丢给 Codex/GPT 自由问答(中文,带 MCP 工具)。"
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
            f"步骤 1: Read 文件 `{PROMPT_MD_PATH}`,定位 '### 大 V X 账号' 节,"
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

    return await _ask_llm(prompt)


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
    return await _ask_llm(prompt)


async def _handle_plan(text: str = "") -> str:
    """
    Ad-hoc enriched execution plan.
      /plan                    — LLM reads latest brief, picks top 3 tickers
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
            f"步骤 0:Read `{Path.home() / 'Reports'}` 目录下**最新**一份 "
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
    return await _ask_llm(intro + body)


async def _handle_ticker(text: str = "") -> str:
    """
    Ad-hoc X discussion for a single ticker (cashtag search).
      /ticker SKM            default: $SKM, past 24h, 15 live tweets
      /ticker SKM 6h         past 6h
      /ticker SKM 30         30 tweets
      /ticker SKM 6h top     'top' (engagement) mode instead of 'live'
    """
    args = text.split()[1:]
    if not args:
        return ("用法: /ticker TICKER [Xh] [N] [top|live]\n"
                "例: /ticker SKM\n"
                "    /ticker INTC 6h\n"
                "    /ticker NVDA 30 top")

    ticker = args[0].upper().lstrip("$")
    if not re.match(r"^[A-Z]{1,5}$", ticker):
        return f"无效 ticker: {args[0]!r} (应是 1-5 字母 ASCII,比如 SKM/INTC/NVDA)"

    window = "24h"
    limit = 15
    mode = "live"
    for a in args[1:]:
        a_low = a.lower()
        if re.match(r"^\d+h$", a_low):
            window = a_low
        elif a_low.isdigit():
            limit = max(1, min(30, int(a_low)))
        elif a_low in ("top", "live"):
            mode = a_low

    prompt = (
        f"步骤 1: `mcp__chatlog__current_time` 拿当前 EDT。\n"
        f"步骤 2: `mcp__twitter__search_tweets query='${ticker}' limit={limit} mode='{mode}'`。\n"
        f"步骤 3: 过滤出过去 {window} 内的推(`posted_at` 对比 since=now-{window})。\n"
        f"步骤 4: 输出中文 markdown:\n\n"
        f"## ${ticker} X 讨论(过去 {window} × mode={mode})\n\n"
        f"### 📈 主流情绪\n"
        f"一句话:看多/看空/分歧/中性 + 大致比例\n\n"
        f"### 🗣️ Top 5 信息量推\n"
        f"按 retweet/like 隐含信号 + 内容独家性排序,每条:\n"
        f"- `HH:MM @user`: 原文 (<100 字) — 一句话解读(catalyst/level/事件)\n\n"
        f"### ⚠️ 红旗 (如有)\n"
        f"shill / pumper / spam account / pre-IPO 投机迹象。无则 skip 整节。\n\n"
        f"### 💡 综合判断\n"
        f"Codex/GPT 给一句话:现在是否值得关注?偏多还是偏空?最重要 catalyst 是什么?\n\n"
        f"如果窗口内 0 条:回 '${ticker} 过去 {window} 在 X 上无讨论'.\n"
        f"输出 < 1500 字。"
    )
    return await _ask_llm(prompt)


def _parse_monitored_sources() -> tuple[list, list]:
    """Read prompt.md tables and return (discord, wechat) lists of tuples:
       discord: [(server / channel name, channel_id), ...]
       wechat:  [(group name, chatroom_id), ...]
    Used by /critique to do stratified random sampling so every source
    eventually gets audited over multiple runs, not just a hardcoded subset.
    """
    discord: list[tuple[str, str]] = []
    wechat: list[tuple[str, str]] = []
    try:
        text = PROMPT_MD_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("could not read prompt.md: %s", exc)
        return discord, wechat

    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("### Discord 频道"):
            section = "discord"; continue
        if s.startswith("### 微信群"):
            section = "wechat"; continue
        if s.startswith("###") or s.startswith("## "):
            section = None
            continue
        if not section or not s.startswith("|"):
            continue
        # Skip header / separator rows
        if "---" in s or "channel_id" in s or "chatroom_id" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if section == "discord" and len(cells) >= 3:
            m = re.match(r"^\d{15,21}$", cells[2])
            if m:
                discord.append((f"{cells[0]} / {cells[1]}", cells[2]))
        elif section == "wechat" and len(cells) >= 2:
            m = re.match(r"^\d+@chatroom$", cells[1])
            if m:
                wechat.append((cells[0], cells[1]))
    return discord, wechat


async def _handle_critique(text: str = "") -> str:
    """Audit the most recent brief for gaps -- signals visible in the raw
    data window but missing from the brief. Reports each gap with a concrete
    prompt.md / memory.md edit proposal. Manual-trigger MVP; weekly auto-run
    may follow once output quality is observed.

    Args after '/critique':
      (none)               -- audit the latest brief in ~/Reports/
      /critique 2          -- audit the brief 2 ago (n-th most recent)
    """
    args = text.split()[1:]
    n_ago = 0
    if args and args[0].isdigit():
        n_ago = int(args[0])

    reports_dir = Path.home() / "Reports"
    briefs = sorted(reports_dir.glob("[0-9]*-[0-9]*-[0-9]*-[0-9]*-brief.md"))
    if not briefs:
        return f"✗ /critique: 在 {reports_dir} 没找到任何 YYYY-MM-DD-HH-brief.md"
    if n_ago >= len(briefs):
        return f"✗ /critique: 只有 {len(briefs)} 份简报存档,n_ago={n_ago} 越界"

    target = briefs[-1 - n_ago]
    # Decode mode/window from the filename hour
    name = target.stem  # "2026-05-17-08-brief"
    try:
        hour = int(name.split("-")[3])
    except (IndexError, ValueError):
        hour = 12  # fallback
    if 0 <= hour < 9:
        window_s, mode = 86400, "盘前 (24h 窗口)"
    elif 9 <= hour <= 16:
        window_s, mode = 5400, "盘中 (90min 窗口)"
    else:
        window_s, mode = 5400, "盘后 (90min 窗口)"

    # Stratified random sample: 3 Discord + 3 WeChat per call. Over multiple
    # /critique runs each source eventually gets covered, vs hardcoded
    # sampling that would systematically miss the unselected sources' gaps.
    discord_all, wechat_all = _parse_monitored_sources()
    sample_d = random.sample(discord_all, min(3, len(discord_all)))
    sample_w = random.sample(wechat_all, min(3, len(wechat_all)))
    sample_lines: list[str] = []
    for i, (name, ch_id) in enumerate(sample_d, 1):
        sample_lines.append(
            f"    {i}. Discord「{name}」"
            f"→ `mcp__discord-selfbot__read_channel_messages(channel_id='{ch_id}', limit=80)` 后过滤 timestamp ≥ since"
        )
    for i, (name, room) in enumerate(sample_w, len(sample_d) + 1):
        sample_lines.append(
            f"    {i}. 微信「{name}」"
            f"→ `mcp__wxstore__wxstore_history(chat='{room}', since=since_dur, until=until_ts, limit=250)`"
        )
    sample_block = "\n".join(sample_lines) if sample_lines else "    (prompt.md 解析失败,fallback 让 LLM 自己读 prompt.md 再采样)"
    sample_summary = (
        ", ".join(d[0] for d in sample_d) + " | " +
        ", ".join(w[0] for w in sample_w)
    ) if sample_lines else "(无)"

    prompt = (
        f"# 简报质量审查任务(stratified-random 采样版)\n\n"
        f"审查最近一份简报有没有漏掉**窗口内可见的明显信号**。**采样策略**:每次随机抽 3 个 Discord 频道 + 3 个微信群("
        f"全集 {len(discord_all)} + {len(wechat_all)}),多次 /critique 后每个源都会被审计到。**这次采样**:{sample_summary}\n\n"
        f"## 步骤\n\n"
        f"**步骤 1**:Read 待审简报 `{target}`(模式:{mode})。\n\n"
        f"**步骤 2**:Read 当前的 `{PROMPT_MD_PATH}` 和 `{MEMORY_MD_PATH}`,知道目前哪些群/频道/大V/watchlist 在跟踪 + 用户的真实角度。\n\n"
        f"**步骤 3**:用 `mcp__chatlog__current_time` 拿当前 EDT 的 unix 秒(now)。**确认简报发布时刻**(`{name}` 时刻,unix 秒,记为 `T`)。**计算两个变量**:\n"
        f"  - `since_dur` = (now - T + {window_s}) 秒 — 从现在回看多远(wxstore_history 的 since 是 duration 字符串)\n"
        f"  - `until_ts` = `T` 的 unix 秒字符串 — 拉到这一刻就停(否则简报发布**之后**的消息也会混进来污染审查)\n"
        f"然后**并行**调用这 6 个采样源(其他源**这次跳过**,下次随机会轮到):\n\n"
        f"{sample_block}\n\n"
        f"**不拉**:大 V 推 / X search / Polymarket 重扫 — 这些信号简报本身已在 Phase B 调过;通过 slash command 重扫只徒增 latency,gap 通常在群讨论里。\n\n"
        f"**步骤 4**:**对比**简报 vs 这 6 个采样源的原始数据,找出**应该收进简报但漏了**的 actionable 信号。**注意采样限制**:本次只覆盖 {len(sample_d)+len(sample_w)} / {len(discord_all)+len(wechat_all)} 个源,未抽中的源的 gap 这次不可见。\n\n"
        f"**步骤 4.5**(跨群验证 — 用语义检索补盲点):对 Step 4 找到的每个候选 gap(尤其是 ticker / KOL 名字 / 题材关键词),调 "
        f"`mcp__wxstore__wxstore_semantic(query='<gap-related keyword>', window='7d', limit=10)` 看其他**未抽中的群**有没有同主题讨论。"
        f"返回 hits 看 sender + content + chat_id:\n"
        f"  - 若多个未抽中群也有相关讨论 → **systemic gap**(简报真的漏了),升级置信度\n"
        f"  - 若只 sampled 群有 → **incidental**,可能这次随机抽样运气好\n"
        f"  - 若 0 hit → 这个 gap 仅 sampled 群内部出现,可能不是 systemic\n"
        f"  - 若返回 `error` 字段(Ollama 暂时不可用之类) → 这步**跳过**,直接走 Step 5 不影响主流程\n\n"
        f"### 算 gap 的标准(必须满足客观可验证)\n"
        f"- **个股漏**:某 ticker 被 **≥3 个不同 sender** (跨群算) 提及,但简报 🎯/⚡ 都没出现\n"
        f"- **仓位漏**:实盘校场/某 KOL 发了**具体 entry/stop/target 或仓位调整数字**,但简报 ⚡ 没提\n"
        f"- **大 V 漏**:prompt.md 跟踪的某大 V 在窗口内发了 actionable 推(具体 ticker / 具体 call / 具体数字),但简报 🎙️ 漏列\n"
        f"  - 注:Cramer/Schiff 是反指,他们 'actionable' 的方向要 flip 后才算\n"
        f"- **技术位漏**:**≥2 个群讨论同一大盘点位**(SPY/QQQ/VIX 整数关、长债 yield 等),但简报 📊 没提\n"
        f"- **宏观漏**:Polymarket 24h 概率剧动 (≥5pp 绝对) + 群里也讨论同一事件,但简报 🏛️ 没体现\n"
        f"- **速读失真**(新加,2026-05-18 起 — 用户在微信只看 📱 微信速读 section):简报里 `## ⚡ 高优先级关注` 下面**已经写了**的 setup,**没出现**在同一份简报的 `## 📱 微信速读` 节里 → 速读压缩遗漏高优 setup。Step 1 Read 待审简报时,**同时定位** `## ⚡` 和 `## 📱` 两个 section,对比:\n"
        f"  · ⚡ 里每个 setup 的 ticker / 立场 / 关键数字,**至少**要在 📱 速读里出现一次 ticker + 立场\n"
        f"  · ⚡ 写「INTC 多」但 📱 完全没提 INTC → **速读失真**\n"
        f"  · ⚡ 写「TSLA 多 entry $XXX」📱 只提了「TSLA」但没立场没数字 → 仍算 partial 失真(置信度=中)\n"
        f"  · 速读本身设计 ≤1900 字,主动 trim 低 conviction setup 是允许的;**只有高 conviction / actionable setup 被砍才是 gap**\n\n"
        f"### **绝不**算 gap 的\n"
        f"- 闲聊 / 段子 / 营销内容\n"
        f"- 单个人单次提到某 ticker\n"
        f"- '简报已经提了但你觉得应该更详细' — 不是漏\n"
        f"- 反指 KOL 喊买你觉得没纳入'共识看多' — 那是用户主动 flip 不是 gap\n\n"
        f"## 输出格式 (严格)\n\n"
        f"```\n"
        f"## 审查 {name} ({mode})\n\n"
        f"### 发现的 gap (N 个)\n"
        f"\n"
        f"- **<类型>**: <具体描述,带具体出处 — 群名 / sender / timestamp 或 X handle / 推文 ID>\n"
        f"  - **建议**: <具体改 prompt.md 的哪一步 / memory.md 的哪一节;用 diff-like 形式描述,如'在 Step 6 的 watchlist 触发条件加: ...'>\n"
        f"  - **置信度**: <高 / 中 / 低>\n"
        f"\n"
        f"### 综合判断\n"
        f"一句话:这次审查发现的 gap 是 systemic (需要改 prompt) 还是 incidental (这一次的偶然)?\n"
        f"```\n\n"
        f"**若无 gap**:直接输出 `## 审查 {name}: 无可执行 gap,本次简报已充分覆盖窗口内信号。`\n\n"
        f"**约束**:\n"
        f"- 整体输出 < 1500 字(微信单条上限)\n"
        f"- 不要写 explanatory 段落、不要寒暄、不要总结改造历史\n"
        f"- 每个 gap 必须可验证(带具体出处),不允许'感觉漏了 XYZ'这种主观判断\n"
        f"- gap 数 0-5 个之间;如果 5+ 表示简报真的漏太多,只列最重要的 5 个,在末尾加 `... 还有 N 个未列`"
        + selfevolve.json_epilogue("critique")
    )
    return await _ask_llm(prompt)


async def _handle_kol_drift(text: str = "") -> str:
    """A: 周度 KOL 漂移检测。对 prompt.md 跟踪的每个大 V 拉过去 30 天推,
    跟当前 memory.md / prompt.md 里的"主战场"对比;**只列 ≥中等漂移**。

    用法:`/kol_drift` (全部 KOL)
    """
    prompt = (
        f"# KOL 主战场漂移审查\n\n"
        f"**步骤 1**:Read `{PROMPT_MD_PATH}` 的'### 大 V X 账号'表 + Read "
        f"`{MEMORY_MD_PATH}`的'KOL 真实角度'章节,知道每个大 V 当前**应该**是什么主战场。\n\n"
        f"**步骤 2**:对表里**每个**大 V 并行调 "
        f"`mcp__twitter__fetch_user_tweets(username=handle, limit=30)`。\n\n"
        f"**步骤 3**:对每个大 V 分析最近 30 条推的**主题分布**:\n"
        f"  - 主要谈什么 ticker / sector / 宏观主题\n"
        f"  - 占比最高的 2-3 个 theme 是什么\n"
        f"  - 跟 prompt.md/memory.md 里描述的'主战场'/'真实角度'比较\n\n"
        f"**步骤 4**:判断漂移程度:\n"
        f"  - **无漂移**:主战场跟描述一致,推占比 60%+ 跟描述对得上\n"
        f"  - **轻微**:60%-30% 跟描述对得上 — **不报**\n"
        f"  - **中等**:<30% 跟描述对得上,新主题占据主导 — **报**\n"
        f"  - **重大**:几乎完全转向 — **报**\n\n"
        f"**步骤 5**:对反指 KOL(Cramer/Schiff)特殊处理:他们 content 永远会变化,**只要**他们仍是"
        f"逆向作用 (consensus 跟实际反向相关) 就不算漂移。如果他们突然变成顺指,那是真漂移要报。\n\n"
        f"## 输出格式 (严格)\n\n"
        f"```\n"
        f"## KOL 漂移审查 ({{日期}})\n\n"
        f"### @handle (drift: 中/重)\n"
        f"- **当前描述**: <copy from prompt.md / memory.md 一句话>\n"
        f"- **实际最近 30 天主战场**: <具体描述 + 引用 1-2 条最 representative 推作为证据>\n"
        f"- **建议新描述**: <新一句话主战场,反映真实焦点>\n"
        f"- **置信度**: 高/中/低\n"
        f"```\n\n"
        f"**全部无 ≥中等漂移**时:输出 `## KOL 漂移审查 ({{日期}}): 全部 KOL 主战场跟当前描述一致,无需更新。`\n\n"
        f"**约束**:\n"
        f"- 总输出 < 1500 字(微信单条上限)\n"
        f"- 只列 ≥中等漂移,**不报**轻微漂移(否则信噪比太低)\n"
        f"- 反指 KOL 仍维持 inverse 关系不算漂移\n"
        f"- 必须用具体推作证据,不允许'感觉漂了'这种主观判断\n"
        f"- 建议描述要可直接 copy 进 prompt.md / memory.md 表里,不要加额外评论"
        + selfevolve.json_epilogue("kol_drift")
    )
    return await _ask_llm(prompt)


async def _handle_heat(text: str = "") -> str:
    """B: Watchlist 热度推荐。读最近 N 份简报(已聚合好的 🎯 section),抽出
    被多次提及但不在 watchlist 的 ticker。**不重新扫原始群消息** — 简报已经
    做过这步聚合,直接消费 brief 结果秒级出结果(~30s vs 6+ min)。

    用法:`/heat` (最近 4 份简报,通常覆盖最近 4 小时盘中 / 1 份盘前 = 24h)
         `/heat 8`  (最近 8 份)
         `/heat 24` (最近 24 份,基本是 1.5 天)
    """
    args = text.split()[1:]
    n_briefs = 4
    if args and args[0].isdigit():
        n_briefs = max(1, min(24, int(args[0])))

    prompt = (
        f"# Watchlist 热度审查\n\n"
        f"**任务**:从最近 {n_briefs} 份简报里抽出**被多次提到但不在 watchlist** 的 ticker,作为加进 watchlist 的候选。"
        f"\n\n**关键**:**不要**重新扫原始群/Discord 消息(那太慢,简报本身就是做这事的)。**只读已聚合的简报文件**。\n\n"
        f"**步骤 1**:Read `{PROMPT_MD_PATH}` 的'### 个股 watchlist'表,记下当前 watchlist 所有 ticker(大写)。\n\n"
        f"**步骤 2**:用 Glob `~/Reports/[0-9]*-[0-9]*-[0-9]*-[0-9]*-brief.md` 找全部简报,按文件名(= 时间)排序,取**最后 {n_briefs} 份**。\n\n"
        f"**步骤 3**:Read 这 {n_briefs} 份简报。从每份的:\n"
        f"  - `## 🎯 个股共识` section 提到的 ticker(典型格式 `**TICKER**(N/M):...`)\n"
        f"  - `## ⚡ 高优先级关注` 里提到的 ticker(setup heading 通常是 `### {{TICKER}} ─ ...`)\n"
        f"  - `## 🎙️ 大 V 速读` 里大 V 推 actionable 提到的 ticker\n"
        f"  - 其他 section 引用的 ticker 顺便扫但权重低\n"
        f"  抽出所有 ticker(大写 1-5 字母,排除 `$USD/$CAD/$EUR/$JPY/$GBP/$CNY` 等假阳性)。\n\n"
        f"**步骤 4**:**对每个 ticker**累计统计:\n"
        f"  - 出现的简报数(N briefs / {n_briefs})\n"
        f"  - 简报里提到的总次数(同一份简报可能 🎯 + ⚡ 都出现 = 2 次)\n"
        f"  - 简报里被引用的源头多样性(群名 / 大V handle)\n\n"
        f"**步骤 5**:过滤:\n"
        f"  - **排除** watchlist 里已有的 ticker\n"
        f"  - **排除**单字母 ticker 和 currency-pseudo($USD/$EUR 等)\n"
        f"  - **保留**至少出现在 **≥{max(2,n_briefs//2)} 份简报**(过半)的 ticker\n\n"
        f"**步骤 6**:按 `brief_count × source_diversity` 降序排,取 top 5。\n\n"
        f"## 输出格式 (严格)\n\n"
        f"```\n"
        f"## Watchlist 热度审查 (最近 {n_briefs} 份简报)\n\n"
        f"### 候选加进 watchlist (按热度):\n"
        f"\n"
        f"1. **$XYZ** ─ 出现 N/{n_briefs} 份,共 M 次提及,跨 K 个源\n"
        f"   - 简报里讨论焦点:<1 句话总结从简报里看到的讨论内容>\n"
        f"   - 出处样本:简报 2026-05-17-08 🎯 / 简报 2026-05-16-14 ⚡ TSLA setup 里提及\n"
        f"   - **建议 thesis**:`<待你 confirm 后补充>`\n"
        f"\n"
        f"2. ... (最多 5 个)\n"
        f"```\n\n"
        f"**全部无符合阈值**时:`## Watchlist 热度审查: 最近 {n_briefs} 份简报无 ≥{max(2,n_briefs//2)}-brief 的 non-watchlist ticker。`\n\n"
        f"**约束**:\n"
        f"- 总输出 < 1500 字\n"
        f"- 最多 5 个候选,严格按热度降序\n"
        f"- 必须 cite 简报文件名 + section,便于你验证\n"
        f"- **绝不**编 thesis 描述(memory.md '行为约束'有规则:generic 描述是历史 bug)\n"
        f"- 你确认要加 watchlist 时,在微信回'个股加 XYZ:<你的真实角度>',listener 走配置管理流程"
        + selfevolve.json_epilogue("heat")
    )
    return await _ask_llm(prompt)


async def _handle_reflect(text: str = "") -> str:
    """Self-reflection: scan recent briefs + logs for repeated patterns
    worth promoting (positive) or avoiding (negative). Surfaces proposals;
    user manually appends to memory.md after review.

    用法: /reflect (默认 7 天) / /reflect 3d / /reflect 14d
    """
    args = text.split()[1:]
    lookback_days = 7
    if args:
        m = re.match(r"^(\d+)d$", args[0].lower())
        if m: lookback_days = max(1, min(30, int(m.group(1))))

    prompt = (
        f"# 自我检讨:近 {lookback_days} 天 brief 运行经验沉淀\n\n"
        f"任务:回顾最近 {lookback_days} 天的 brief 运行,找出**可以沉淀到 memory.md 的运行经验** — "
        f"正向(发现的好做法)+ 负向(踩过的坑)。**只列 ≥2 次重复出现**的模式,避免单次偶然。"
        f"\n\n## 步骤\n\n"
        f"**1.** Read 当前 `{MEMORY_MD_PATH}` 的'## 运行经验:正向'和'## 运行经验:负向'两节,**列出已有条目**避免重复沉淀。\n\n"
        f"**2.** Glob `~/Reports/[0-9]*-[0-9]*-[0-9]*-[0-9]*-brief.md` 找最近 {lookback_days} 天的 brief 文件(每天约 15 份,共 ~{lookback_days*15} 份)。\n\n"
        f"**3.** Glob `~/Scripts/market-brief/logs/[0-9]*-[0-9]*-[0-9]*.log` 找最近 {lookback_days} 天的日志(每天 1 份)。\n\n"
        f"**4.** **抽样 Read**(全量太大):\n"
        f"  - 简报:每天随机 1-2 份(覆盖盘前/盘中/盘后各类型),共 ~{lookback_days * 2} 份\n"
        f"  - 日志:全部读完({lookback_days} 份小文件)\n\n"
        f"**5.** **找模式**:\n\n"
        f"  **正向候选(好做法值得沉淀)**:\n"
        f"  - 某 prompt step 处理方式效果好(如 multi-wave pagination 减少 grep fallback)\n"
        f"  - 某 MCP 调用搭配产生有用结果(如 implied_move + unusual_activity 一起看)\n"
        f"  - LLM 在 brief 里展示的 emergent 自适应行为\n"
        f"  - 某种数据源结合得出特别准的信号\n\n"
        f"  **负向候选(避坑)**:\n"
        f"  - 某种调用反复失败(token-cap / timeout / rate-limit)\n"
        f"  - 某种 prompt 写法导致 LLM 困惑或污染输出\n"
        f"  - 某数据源持续不稳定 / 噪音多\n"
        f"  - 某种 sentiment 判断被反转后才对(没考虑到的反指)\n\n"
        f"**6.** **过滤**:\n"
        f"  - 必须 ≥2 次实际观察,引用具体 brief 文件名 / 日志行\n"
        f"  - 必须**不在**memory.md 已有'运行经验'里(若类似但不同,合并改进而非新增)\n"
        f"  - 置信度低 / 单次观察 / 推测性 — **一律不报**\n\n"
        f"## 输出格式(严格)\n\n"
        f"```\n"
        f"## 自我检讨 ({{今日日期}}, 近 {lookback_days} 天)\n\n"
        f"### 已有 memory 经验:正向 X 条 / 负向 Y 条\n\n"
        f"### 新发现 — 正向候选\n\n"
        f"1. **<简称>** [置信度: 高/中]\n"
        f"   - **观察**:`<具体 brief / 日志 / 行号>` × **≥2 次**\n"
        f"   - **建议沉淀**(追加到 memory.md '## 运行经验:正向'节末尾,一行):\n"
        f"     ```\n"
        f"     - **{{今日}}**:<具体规则,操作 + 适用条件>\n"
        f"     ```\n\n"
        f"### 新发现 — 负向候选\n\n"
        f"1. **<简称>** [置信度: 高/中]\n"
        f"   - **观察**:`<具体出处>` × **≥2 次失败**\n"
        f"   - **建议沉淀**('## 运行经验:负向'节末尾):\n"
        f"     ```\n"
        f"     - **{{今日}}**:<什么动作会失败 + 应该用什么替代>\n"
        f"     ```\n\n"
        f"### 应该淘汰的已有经验(过期/被推翻,可选)\n\n"
        f"- `<memory.md 现有的某条>` 不再适用 → 建议删除\n"
        f"  - **理由**:<什么变了 / 新证据>\n\n"
        f"### 综合建议\n"
        f"一句话:新增 X 条正向 + Y 条负向 + Z 条淘汰。**人工 review 后**手动追加 / 删除到 memory.md\n"
        f"```\n\n"
        f"**无新发现**时:`## 自我检讨:近 {lookback_days} 天观察跟 memory.md 已有规则一致,无需更新。`\n\n"
        f"**约束**:\n"
        f"- 总输出 < 1800 字(可能切 2 条微信)\n"
        f"- 每条候选 ≥2 个观察依据,**否则不报**(单次偶然不沉淀)\n"
        f"- 已在 memory.md 的不要重复报\n"
        f"- 不允许 fabrication,只基于实际看到的 brief / log 内容\n"
        f"- 优先**操作级**经验(具体规则、limit 数字、文件路径),不要写抽象哲学"
        + selfevolve.json_epilogue("reflect")
    )
    return await _ask_llm(prompt)


async def _handle_score(text: str = "") -> str:
    """C: 命中率记分。统计过去 N 天盘前简报里 ⚡ section 推荐的实际表现。

    用法:`/score` (默认 7 天 / horizon 3d) / `/score 14d 1w` 等
    """
    args = text.split()[1:]
    lookback_days = 7
    horizon = "3d"
    if args:
        m = re.match(r"^(\d+)d$", args[0].lower())
        if m: lookback_days = int(m.group(1))
    if len(args) > 1 and re.match(r"^\d+[dwh]$", args[1].lower()):
        horizon = args[1].lower()

    prompt = (
        f"# ⚡ Setup 命中率审查\n\n"
        f"**窗口**:过去 **{lookback_days} 天**的盘前简报;**评估 horizon**:{horizon}\n\n"
        f"**步骤 1**:`ls ~/Reports/` 找最近 {lookback_days} 天的盘前简报。文件名格式 `YYYY-MM-DD-HH-brief.md`,**只取 HH < 09** 的(盘前)。逐份 Read。\n\n"
        f"**步骤 2**:从每份简报的 `## ⚡ 高优先级关注` section 提取每个 setup 的元组 `(ticker, brief_publish_time, 立场[多/空/观望], target?, source[群/大V/Polymarket])`:\n"
        f"  - 立场写在'**可执行**'子节的'立场:多/空/观望'\n"
        f"  - source 从'**消息面/基本面**'子节里 actionable 推/群消息的 @ 或群名提取\n"
        f"  - **观望**不参与命中率统计,跳过\n\n"
        f"**步骤 3**:对每个 (ticker, brief_time) 调 `mcp__stock-price__check_post_hoc(ticker, at_time=brief_time, horizon='{horizon}')`(可并行)。拿 `net_move_pct`。\n\n"
        f"**步骤 4**:判 hit:\n"
        f"  - 立场=多 且 `net_move >= +1.0%` → **win**\n"
        f"  - 立场=多 且 `net_move <= -1.0%` → **loss**\n"
        f"  - 立场=空 且 `net_move <= -1.0%` → **win**\n"
        f"  - 立场=空 且 `net_move >= +1.0%` → **loss**\n"
        f"  - `-1.0% < net_move < +1.0%` → **flat**(不计入)\n\n"
        f"**步骤 5**:聚合统计(**多维度交叉看**):\n"
        f"  - **整体**:W / L / Flat,胜率 = W / (W + L)\n"
        f"  - **按 ticker**:每个 ticker 的 setup 次数 + W/L/F + 胜率(只列 ≥2 个 setup 的)\n"
        f"  - **按大 V 关联**:setup 的 source 里跟踪的大 V (查 prompt.md 表),分别统计相关 ticker 的胜率;反指 KOL (Cramer/Schiff) 把方向 flip 后再算\n"
        f"  - **按 source_kind**(新加,2026-05-18 起):判断每个 setup 的数据来源类型并分别看胜率:\n"
        f"    · `text` — setup 来自群文字/X 推/Polymarket 数字,无 OCR 介入\n"
        f"    · `ocr`  — setup 主要依据是 OCR 解读的图片(实盘校场截图、月哥 broker 截图、郭明錤长图等)\n"
        f"    · `cross_validated` — ≥2 个独立源(text + ocr / 2 个不同群 / 实盘校场 + 期权 unusual activity)同向 confirm\n"
        f"    · `n/a` — 判断不出的,跳过不归类\n"
        f"    源头分类**从简报里找**:`### TICKER` 下面的'消息面/基本面'子节里是否引用了'实盘校场截图''broker 仓位图''看图解析'等关键词 → ocr;只有文字 quote → text;明确两个独立源 confirm → cross_validated\n"
        f"  - **按 window_aware**(新加):观月亭/小林ablue 等时段性源(memory.md '观月亭🌒' 一段有定义)关联的 setup 单独看 — 这些数据源 absent ≠ failure,胜率不能跟 always-on 源直接比\n\n"
        f"## 输出格式 (严格)\n\n"
        f"```\n"
        f"## ⚡ Setup 命中率 (最近 {lookback_days} 天,horizon={horizon})\n\n"
        f"### 整体\n"
        f"- 多:**W**/**L**/**F** → 胜率 **X%**\n"
        f"- 空:**W**/**L**/**F** → 胜率 **X%**\n"
        f"- 综合:**X%** 胜率(共 **N** 个 actionable setup,**M** 个 flat 不计)\n"
        f"\n"
        f"### 按 ticker (≥2 setup 的)\n"
        f"- **$XYZ**:N setup,W-L-F → 胜率\n"
        f"- ... (按 setup 数 × 胜率排,top 5)\n"
        f"\n"
        f"### 按 source_kind\n"
        f"- **text**(N=X):W-L-F → 胜率 X%\n"
        f"- **ocr**(N=X):W-L-F → 胜率 X%\n"
        f"- **cross_validated**(N=X):W-L-F → 胜率 X%\n"
        f"  - 注:cross_validated 胜率应该 ≥ text/ocr 单独;若不是,说明 cross-validation 信号选择有偏差,需 reflect\n"
        f"\n"
        f"### 按大 V 关联\n"
        f"- **@imnotharsh** (INTC bull):N 个相关 setup,胜率 X%\n"
        f"- **@Cramer-flipped**:... (注:Cramer 反指后看)\n"
        f"- ... (只列 ≥2 setup 的)\n"
        f"\n"
        f"### 建议\n"
        f"一句话:**信号金字塔**应该调整哪些权重?哪些 ticker / KOL 命中率持续低需要重新审视 thesis?source_kind 之间胜率有显著差距吗(暗示某种来源系统性失真)?\n"
        f"```\n\n"
        f"**数据不足**(N < 5 actionable setup)时:输出 `## 命中率:数据不足({{N}} 个 setup,至少需要 5 个才有统计意义)。继续观察。`\n\n"
        f"**约束**:\n"
        f"- 总输出 < 1500 字\n"
        f"- 数字必须真实(从 check_post_hoc 实际返回值算),不允许估算\n"
        f"- 标注 sample size,N < 5 直接说不足而非硬编故事\n"
        f"- 反指 KOL 一定要 flip 后算 — 否则结果会被错误负相关污染\n"
        f"- source_kind 判断**只 based on 简报里实际写的内容**,不要根据训练数据脑补「这种 ticker 通常 ocr」"
        + selfevolve.json_epilogue(
            "score",
            stats_schema=(
                "{\n"
                '    "overall": {"long": {"w": 0, "l": 0, "f": 0, "win_rate": 0.0},\n'
                '                "short": {"w": 0, "l": 0, "f": 0, "win_rate": 0.0},\n'
                '                "combined": {"n_actionable": 0, "n_flat": 0, "win_rate": 0.0}},\n'
                '    "by_ticker": [{"ticker": "INTC", "n": 0, "w": 0, "l": 0, "f": 0, "win_rate": 0.0}, "..."],\n'
                '    "by_kol":    [{"handle": "imnotharsh", "is_inverse": false, "n": 0, "w": 0, "l": 0, "win_rate": 0.0}, "..."],\n'
                '    "by_source_kind": {"text":            {"n": 0, "w": 0, "l": 0, "f": 0, "win_rate": 0.0},\n'
                '                       "ocr":             {"n": 0, "w": 0, "l": 0, "f": 0, "win_rate": 0.0},\n'
                '                       "cross_validated": {"n": 0, "w": 0, "l": 0, "f": 0, "win_rate": 0.0}},\n'
                '    "window_aware_sample_size": 0\n'
                "  }"
            ),
        )
    )
    return await _ask_llm(prompt)


async def _handle_search(text: str = "") -> str:
    """语义检索:跨所有微信群的历史向量搜索。

    用法:
      /search INTC 加仓        — 默认 30d 窗口
      /search 30d INTC 加仓    — 指定窗口 (1d/7d/30d/90d/1y)
      /search 牛哥 ASTS 评估   — 自然语言查询,不需要 cashtag
    """
    args = text.split(maxsplit=2)
    if len(args) < 2:
        return ("用法:`/search [window] <query>`,例:\n"
                "  `/search INTC 加仓`(默认 30d)\n"
                "  `/search 7d 通胀 6%`(自定义窗口)\n"
                "  `/search 90d 牛哥 ASTS`(自然语言)")
    window_re = re.compile(r"^(\d+)([dwy])$|^1y$")
    if len(args) >= 3 and window_re.match(args[1]):
        window = args[1]
        query = args[2]
    else:
        window = "30d"
        query = " ".join(args[1:])

    prompt = (
        f"# 跨群语义检索任务\n\n"
        f"用户查询:**{query}**(窗口:{window})\n\n"
        f"**步骤 1**:调 `mcp__wxstore__wxstore_semantic(query='{query}', window='{window}', limit=20)` —— wxstore 本机 sqlite-vec + Ollama qwen3-embedding,**这是当前 RAG 主存储**。\n\n"
        f"**步骤 2**:处理返回结果:\n"
        f"  - 若返回 JSON 含 `error` 字段(Ollama 暂时不可用 / wxstore 服务挂等)→ 告诉用户'语义检索暂时不可用(<error 内容>),降级到关键词搜索'。然后调 `mcp__chatlog__wx_search(keyword='<extract main keyword>', limit=15)` 作为 fallback —— 注意这是 chatlog 元数据查询,不抢锁\n"
        f"  - 若返回 `hits` 数组 → 按下面格式展示。每条 hit 含 `chat`/`chat_name`/`sender_name`/`ts`/`text`/`distance`(距离越小越相关)\n\n"
        f"**步骤 3**:用 `mcp__chatlog__current_time` 拿当前时间(为相对时间表达)。\n\n"
        f"## 输出格式 (严格)\n\n"
        f"```\n"
        f"## 语义检索:{query}(窗口 {window})\n\n"
        f"找到 N 条相关消息(按相关度排序):\n\n"
        f"### 1. <群名>(<相对时间,如 '2 天前 21:30'>)\n"
        f"  **{{sender}}**: {{content 前 150 字,长截断}}\n"
        f"\n"
        f"### 2. ...(以此类推,最多 10 条)\n"
        f"\n"
        f"### 综合\n"
        f"一句话总结:这些讨论的核心观点 / 是否有 actionable signal / 涉及哪些 ticker。\n"
        f"```\n\n"
        f"**约束**:\n"
        f"- 总输出 < 1500 字(微信单条上限)\n"
        f"- 同一群多条 hit 时合并到同一 section,按时间倒序\n"
        f"- emoji/sticker/纯链接 hit 跳过不展示\n"
        f"- 若 0 hit → 直接说'窗口内无相关讨论',不要硬编"
    )
    return await _ask_llm(prompt)


# ────────────────────────────────────────────────────────────────────────────
#  Phase 2c+2d: applier slash commands (control-plane, no LLM needed)
# ────────────────────────────────────────────────────────────────────────────

# Lazy import so a broken applier.py can't take down the listener at startup.
def _applier():
    import applier  # noqa: WPS433
    return applier


async def _handle_proposals(text: str = "") -> str:
    """List pending self-evolve proposals. Optional Nd lookback (default 30d).

    用法:`/proposals` (近 30 天)  /  `/proposals 7d`
    """
    args = text.split()[1:]
    days = 30
    if args:
        m = re.match(r"^(\d+)d$", args[0].lower())
        if m:
            days = max(1, min(90, int(m.group(1))))
    return _applier().list_pending(max_age_days=days)


async def _handle_show(text: str = "") -> str:
    """显示某个 proposal 的完整内容 + diff preview。

    用法:`/show <prop_id>` (id 是 /proposals 列表里第一段那串)
    """
    args = text.split()[1:]
    if not args:
        return "用法:`/show <prop_id>` —— id 是 /proposals 列出的那串(如 `20260518T205500_reflect_01_d671`)"
    return _applier().show_proposal(args[0].strip("`"))


async def _handle_apply(text: str = "") -> str:
    """落盘一个 proposal:备份 target file,append patch 到指定 section,
    把 proposal 文件移到 applied/。仅适用于 writable=true (即 memory.md
    的 ## 运行经验:正向/负向 + ## 信号优先级金字塔, 或 prompt.md 的
    ## 行为约束)。

    用法:`/apply <prop_id>`
    """
    args = text.split()[1:]
    if not args:
        return "用法:`/apply <prop_id>` —— 先 `/proposals` 列出待审,再 `/show <id>` 看 diff,确认后再 /apply"
    return _applier().apply_proposal(args[0].strip("`"))


async def _handle_reject(text: str = "") -> str:
    """拒绝一个 proposal,移到 rejected/。

    用法:`/reject <prop_id>`
    """
    args = text.split()[1:]
    if not args:
        return "用法:`/reject <prop_id>`"
    return _applier().reject_proposal(args[0].strip("`"))


async def _handle_rollback(text: str = "") -> str:
    """回退 memory.md 或 prompt.md 到最近一次 backup。

    用法:`/rollback memory.md`  或  `/rollback prompt.md`
    """
    args = text.split()[1:]
    if not args:
        return "用法:`/rollback memory.md` 或 `/rollback prompt.md` —— 恢复到最近一次 apply 之前的版本"
    return _applier().rollback_file(args[0].strip("`"))


COMMANDS = {
    "/ping": _handle_ping,
    "/brief": _handle_brief,
    "/dv": _handle_dv,
    "/xfeed": _handle_xfeed,
    "/plan": _handle_plan,
    "/ticker": _handle_ticker,
    "/critique": _handle_critique,
    "/kol_drift": _handle_kol_drift,
    "/heat": _handle_heat,
    "/score": _handle_score,
    "/reflect": _handle_reflect,
    "/search": _handle_search,
    # Phase 2c+2d applier control plane
    "/proposals": _handle_proposals,
    "/show":      _handle_show,
    "/apply":     _handle_apply,
    "/reject":    _handle_reject,
    "/rollback":  _handle_rollback,
    "/help": _handle_help,
}


# ────────────────────────────────────────────────────────────────────────────
#  LLM dispatch
# ────────────────────────────────────────────────────────────────────────────

def _llm_backend() -> str:
    return (os.environ.get("MARKET_BRIEF_LLM_BACKEND") or "codex").strip().lower()


async def _ask_llm(user_text: str) -> str:
    backend = _llm_backend()
    if backend in {"codex", "gpt", "openai"}:
        return await _ask_codex(user_text)
    if backend == "claude":
        return await _ask_claude(user_text)
    return f"❌ 未知 MARKET_BRIEF_LLM_BACKEND={backend!r}; 支持 codex 或 claude"


async def _ask_codex(user_text: str) -> str:
    """Run `codex exec` and return only the model's final message."""
    prompt = SYSTEM_PROMPT + user_text.strip()
    last_message = LOG_DIR / f"codex_last_message_{uuid.uuid4().hex}.txt"
    cmd = [
        os.environ.get("COMSPEC", "cmd.exe"),
        "/c",
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "-C", str(SCRIPTS_DIR),
        "--output-last-message", str(last_message),
        "--color", "never",
    ]
    model = os.environ.get("MARKET_BRIEF_CODEX_MODEL", "gpt-5.4").strip()
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=LLM_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        last_message.unlink(missing_ok=True)
        return f"❌ Codex/GPT 超时 ({LLM_TIMEOUT_S}s 无回应)"

    stdout_text = stdout.decode("utf-8", "replace").strip()
    stderr_text = stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        last_message.unlink(missing_ok=True)
        err_tail = (stderr_text or stdout_text)[-800:]
        return f"❌ Codex/GPT 异常 (exit {proc.returncode}):\n{err_tail}"

    reply = ""
    try:
        if last_message.exists():
            reply = last_message.read_text(encoding="utf-8").strip()
    finally:
        last_message.unlink(missing_ok=True)
    return reply or stdout_text or "(Codex/GPT 返回空)"


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
            timeout=LLM_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return f"❌ Claude 超时 ({LLM_TIMEOUT_S}s 无回应)"

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
        # Phase 2b: self-evolve commands also emit a <proposals>JSON</proposals>
        # block. Extract + persist proposals; strip the block from the reply
        # so the WeChat user only sees the markdown. Non-self-evolve commands
        # have no JSON block; extract_proposals_block returns reply unchanged.
        if cmd_key in SELF_EVOLVE_COMMANDS:
            try:
                cleaned, proposals_payload = selfevolve.extract_proposals_block(reply)
                if proposals_payload is not None:
                    written = selfevolve.save_proposals(cmd_key.lstrip("/"), proposals_payload)
                    log.info(
                        "selfevolve: %s emitted %d proposal(s) → %s",
                        cmd_key, len(written),
                        ", ".join(p.name for p in written) or "(empty)",
                    )
                else:
                    log.info("selfevolve: %s did not emit a parseable <proposals> JSON", cmd_key)
                reply = cleaned
            except Exception:
                log.exception("selfevolve: extraction/persist failed for %s", cmd_key)
        # Persist the output so a follow-up free-form message ("按你建议加",
        # "刚才你说什么", etc.) can reconstruct what the previous slash
        # command actually said. Each message spawns a fresh LLM session
        # session with no shared memory; on-disk persistence is the bridge.
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S EDT", time.localtime())
            out_path = SCRIPTS_DIR / f"last_{cmd_key.lstrip('/')}.md"
            out_path.write_text(
                f"# Last `{cmd_key}` output ({stamp})\n\n"
                f"User typed: `{text}`\n\n"
                f"---\n\n{reply}\n",
                encoding="utf-8",
            )
        except Exception:
            log.exception("could not persist %s output", cmd_key)
    else:
        reply = await _ask_llm(text)

    await _push_reply(creds, reply)
    log.info("replied: %r", reply[:120])


async def _fetch_typing_ticket(session, creds) -> str | None:
    """Best-effort getconfig -> typing_ticket. None on any failure."""
    try:
        resp = await _get_config(
            session,
            base_url=creds["base_url"],
            token=creds["token"],
            user_id=creds["home_channel"],
            context_token=None,
        )
    except Exception as exc:
        log.warning("keepalive: getconfig failed: %s", exc)
        return None
    if resp.get("ret") not in (0, None):
        log.warning("keepalive: getconfig ret=%s msg=%s",
                    resp.get("ret"), resp.get("errmsg"))
        return None
    ticket = str(resp.get("typing_ticket") or "")
    return ticket or None


async def _keepalive_loop(creds, session) -> None:
    """Periodically send a typing start+stop pair to user's chat. Hypothesis:
    iLink treats typing pings as activity that keeps the outbound session
    fresh without consuming sendmessage quota. Status=1 then 0 within 200ms
    so the user never visibly sees a lingering 'typing...' indicator.

    First fire after one full interval (not at startup) so a daemon restart
    doesn't immediately ping. Wrapped in broad try/except: this is a
    best-effort experiment, must not crash the listener.
    """
    if KEEPALIVE_INTERVAL_S <= 0:
        log.info("keepalive: disabled (KEEPALIVE_INTERVAL_S=0)")
        return
    log.info("keepalive: typing-ping every %d s to %s",
             KEEPALIVE_INTERVAL_S, creds["home_channel"][:8] + "…")
    try:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            ticket = await _fetch_typing_ticket(session, creds)
            if not ticket:
                log.warning("keepalive: no typing_ticket -- skipping this round")
                continue
            try:
                await _send_typing(
                    session, base_url=creds["base_url"], token=creds["token"],
                    to_user_id=creds["home_channel"], typing_ticket=ticket, status=1,
                )
                await asyncio.sleep(0.2)
                await _send_typing(
                    session, base_url=creds["base_url"], token=creds["token"],
                    to_user_id=creds["home_channel"], typing_ticket=ticket, status=0,
                )
                log.info("keepalive: typing-ping OK")
            except Exception as exc:
                log.warning("keepalive: typing-ping send failed: %s", exc)
    except asyncio.CancelledError:
        log.info("keepalive: cancelled (listener shutting down)")
        raise


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
        keepalive_task = asyncio.create_task(_keepalive_loop(creds, session))
        try:
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
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except (asyncio.CancelledError, Exception):
                pass


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main_loop()))
    except KeyboardInterrupt:
        log.info("interrupted; exiting")
        sys.exit(0)
