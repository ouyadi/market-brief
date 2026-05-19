# market-brief

> 一个 Claude Code skill + Windows/macOS 定时任务,每小时(美东 08:00–22:00)
> 扫描你的 Discord 和微信投资群,生成分级 markdown 简报,推送到你的微信
> (主通道) + 邮箱兜底。**可选**开一个长轮询 listener,直接在微信里跟
> Claude 对话(并用同一套 MCP 工具)。

## 它做什么

### 定时外推(主链路)

```
Task Scheduler / launchd  (美东 08:00–22:00,每小时)
        ↓
run.ps1 / run.sh
        ↓
claude --print  (prompt.md → 调用 mcp__chatlog + mcp__discord-selfbot + mcp__twitter + mcp__stock-price)
        ↓
Reports/YYYY-MM-DD-HH-brief.md   (完整简报存盘)
        ↓
push_weixin.py --section ⚡ --section 🎯 --section 🎙️
        ↓
Hermes Agent → iLink → 你的微信 (3 条独立消息)
        ↓ (推送失败时)
SMTP → Gmail → 邮箱 (完整简报)
```

- **分时段感知**:盘前(扫过去 24h)/盘中(90min)/盘后(90min),`prompt.md` 按美东时间自动切结构
- **推 3 个核心 section**:`⚡ 高优先级关注` + `🎯 个股新动向` + `🎙️ 大 V 速读`,各一条微信消息。完整报告还在盘上 + 邮箱兜底里
- **智能切分**:超长 section 自动按 H2/H3 标题边界切,绝不把 `### TSLA` 标题孤立在上一条末尾(详见 [`push_weixin.py`](mcp/push_weixin.py) 的 `smart_chunks`)
- **失败邮件兜底**:微信是主通道,只有 push 失败才发邮件。Hermes 已经做了 tokenless retry,实测 30 天 0 失败

### 可选:微信入站监听(跟 bot 对话)

```
你在微信发消息给 bot
        ↓
WeixinListener 任务(At log on,run-listener.ps1 / launchd)
        ↓
listen_weixin.py 长轮询 iLink /getupdates
        ↓
  11 个斜杠命令(便宜,不耗 Claude):
    基础: /ping /brief /help
    分析: /dv [handle] [Xh]    大 V 速读
          /xfeed [tab] [N]     X 个人时间线
          /plan [tickers]      可执行方案富化(stock-price + X news)
          /ticker TICKER       单股 cashtag 搜
    self-evolution(meta 维护工具):
          /critique [N]        审最近简报 vs raw data 找 gap
          /kol_drift           检测 KOL 主战场漂移,提议更新描述
          /heat [N]            从最近 N 份简报抽未在 watchlist 的高频 ticker
          /score [Nd] [hzn]    ⚡ setup 命中率统计
          /reflect [Nd]        自我检讨 ≥2 次重复出现的运行经验,提议沉淀
  其他自由文本 → claude --print --dangerously-skip-permissions
                  (跟定时任务同一套 MCP 工具,你可以问
                   "月哥下午说啥了" / "现在 TSLA 多少人在喊买" 这种 ad-hoc)
        ↓
send_weixin_direct → 回到同一个微信对话
```

Listener 严格过滤:只响应 bot owner 自己(`WEIXIN_HOME_CHANNEL`),陌生人 DM 不会触发回复。

后台还有一个 **typing-ping keepalive**(默认 30 分钟一次),用 iLink 的 typing 接口当 session 心跳,**不消耗 sendmessage quota**、用户看不到。可通过 `KEEPALIVE_INTERVAL_S=0` 关闭。

### 可选:股价 + 期权 MCP(yfinance)

HTTP MCP server `stock_price_mcp.py`(端口 3032),基于 `yfinance`。免费、无 auth。**9 个工具,分两组**:

**价格 / 基本面 (4 个)**:
- **`get_quote(ticker)`** — 实时价/涨跌幅/成交量/市值/52w 高低
- **`get_history(ticker, period, interval)`** — OHLCV 时序(最多 50 根)
- **`get_info(ticker)`** — sector / forward_pe / next_earnings_date / dividend / 业务概览
- **`check_post_hoc(ticker, at_time, horizon)`** — 事后微观回测:tweet 时间点 + horizon → price_at_time / max_gain / max_drawdown / net_move。**评 KOL/群主 call 命中率**用

**期权(5 个,含 Black-Scholes Greeks)**:
- **`list_expirations(ticker)`** — 所有可交易到期日
- **`get_option_chain(ticker, expiration, contract_type, near_strike_pct, with_greeks)`** — 链条数据,默认 ±10% near spot + 自带 Δ/Γ/Θ/Vega/Rho
- **`implied_move(ticker, expiration)`** — ATM straddle / spot = 期权市场定的 ±X% + 上下 breakeven。**财报 setup 核心指标**
- **`unusual_activity(ticker, expiration, min_vol_oi_ratio)`** — volume >> open interest 的 strikes,**交叉验证群里说的"$XXX call 大单"**
- **`compute_greeks(ticker, expiration, strike, contract_type)`** — 单 strike 希腊字母(ad-hoc)

Greeks 走纯 stdlib Black-Scholes(`math.erf` 算 normal CDF),不依赖 scipy。无风险利率默认 4.5%,可通过 `STOCK_MCP_RISK_FREE_RATE` 环境变量或 per-call 参数覆盖。

yfinance 抓 Yahoo Finance,>10 次/分钟可能触发短暂限速,每小时 brief 完全没问题。Yahoo 在冷门 strike 上 IV 可能 stale,所以零成交量 strike 的 Greeks 按近似看。

后台守护:scheduled task `StockPriceMCP`(at log on,hidden)/ launchd `com.ouyadi.stock-mcp`。

### 可选:Polymarket 预测市场 MCP

第四个 HTTP MCP server(`polymarket_mcp.py`,端口 3033),基于 Polymarket 的 **Gamma API + CLOB API**。**预测市场价格 = 市场隐含概率**,money-weighted、实时,比新闻头条更早动。免费、无 auth。**9 个工具,分两组**:

**Gamma 发现层(5 个)**:
- **`list_markets(query, limit, active_only, sort_by)`** — 市场列表,query 模糊匹配 question
- **`get_market(id_or_slug)`** — 单市场完整字段
- **`list_events(query, limit, active_only)`** — 事件列表(一个事件聚合多个相关市场,比如 "Fed 6 月会议" 下挂 25bp/50bp/hold)
- **`get_event(id_or_slug)`** — 单事件 + 全部子市场
- **`top_movers(window='1mo', limit)`** — 月度概率变动最大(Gamma 没暴露 1d/1w 价变)

**CLOB 时间序列层(4 个,补 Gamma 的短窗口空白)**:
- **`get_price_history(id_or_slug, lookback_hours, fidelity_minutes)`** — 任意窗口 P(t) 时序
- **`prob_change(id_or_slug, lookback)`** — `'1h' / '24h' / '7d' / '1mo'` 任意窗口 Δ,**这是日常用最多的工具**
- **`compute_vol(id_or_slug, lookback_days)`** — log-return 年化波动率(非 BS-IV,解读为市场分歧度)
- **`short_movers(window_hours, limit, scan_size)`** — 1h/24h/7d 真正短窗口 movers(CLOB 时序算)

简报用法:Fed 决议 / CPI 数据 / 大选辩论 / 地缘事件 / 加密里程碑 都是 Polymarket 重点市场。**跟群里 / KOL 的 call 交叉验证特别有用**:群里说"肯定降 50bp"vs Polymarket 实际定价 30% → 信号偏差量化。

跑在 `127.0.0.1:3033/mcp`,scheduled task `PolymarketMCP` / launchd `com.ouyadi.polymarket-mcp`。**只读**,纯 HTTP GET,Polygon 链上的下单部分(CLOB 写入)我们不暴露。

### 可选:FinancialJuice 实时财经/地缘 wire MCP

HTTP MCP server `financialjuice_mcp.py`(端口 3034),基于 [financialjuice.com](https://www.financialjuice.com) 的公开 RSS feed。**实时财经 + 地缘 wire**,免费无 auth。daemon 自动每 5 min poll 一次,本地 `cache.jsonl` 累计 deduped headlines(典型 12-36h 跨度),解决 RSS snapshot 只暴露 ~100 条的限制。**4 个工具**:

- **`list_headlines(since, limit, query, tag)`** — 主查询,since `'1h' / '24h' / '7d'` 等;tag 可选 `fed / macro / trump / geopolitics / earnings / crypto / ipo / china`
- **`get_tagged(tag, limit, since)`** — 按 tag 速取
- **`get_for_ticker(ticker, since, limit)`** — `$TICKER` 命中(wire 用公司名 > $cashtag,命中可能少但偶有)
- **`cache_status()`** — debug,看 cache 数 + tag 分布

入库时自动 regex-tag(地缘 / Fed / Trump / CPI/PPI / earnings / 加密 / IPO / 中国)。简报用法:跟 Polymarket 配对组成 **"事件 + 市场定价"** 双视角。"Saudi 谴责无人机袭击 Barakah" (FJ) + "Hormuz 5/31 前解封概率 17.5%(月 Δ -70pp)" (Polymarket) 并排 → 一眼看出"事件升级 + 市场已 price in"。

跑在 `127.0.0.1:3034/mcp`,scheduled task `FinancialJuiceMCP` / launchd `com.ouyadi.financialjuice-mcp`。Poll 间隔可通过 `FINANCIALJUICE_POLL_S` 环境变量调(默认 300s)。

### 可选:X (Twitter) MCP via Playwright

主流自部署 X 爬虫库(`twscrape`、`agent-twitter-client`等)都被 X 反爬打死了。**本仓附带**一个基于 Playwright 的 HTTP MCP server(`twitter_playwright_mcp.py`),启动 **用户本机装的 Chrome** 并注入你的 X cookies,直接抓 DOM。暴露:

- `fetch_tweet_by_url(url, include_thread=True)` — 单条推 + **自动展开 thread**(如果是 thread head)
- `fetch_thread(url, max_tweets=20)` — 显式抓 thread:从指定推开始,沿同作者 self-reply 链向下走
- `fetch_user_tweets(username, limit)` — @某用户最近 N 条
- `search_tweets(query, limit, mode)` — `live` (最新) 或 `top` (热门) 搜索
- `fetch_home_timeline(tab='for_you'|'following', limit)` — 你自己 X 首页两个 tab

四种 X 推内容形态全覆盖:
| 形态 | 处理 |
|---|---|
| 普通短推 (≤280) | 直接抓 |
| Premium 长推 (≤25K) | 自动点 "Show more" 展开 |
| Thread (连续 self-reply) | `_collect_self_replies` 串成链 |
| 图片/视频推 | `media[]` 字段返 URL → Claude WebFetch + vision 读 |

跑在 `127.0.0.1:3031/mcp`,scheduled task `TwitterMCP` / launchd `com.ouyadi.twitter-mcp`。**只读** — 仅 `fetch_*` / `search_*`,主账号封号风险最低。

为什么"可选":需要你本机有 Chrome + 一次性导出 cookies。Cookies 存在 `~/twitter-mcp/.env`(gitignored)。详见 SKILL.md Step 10。

## 安装

### Windows:一键安装(推荐)

前置:已经装了本仓 [`chat-mcp-setup/`](chat-mcp-setup/) 下的 `chatlog` + `discord-selfbot` MCP servers(见下面 *前置条件*)。

```powershell
# 1. 克隆本仓
git clone https://github.com/ouyadi/market-brief.git $env:USERPROFILE\market-brief

# 2. 一行命令 —— 安装器分 7 个幂等阶段
cd $env:USERPROFILE\market-brief
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\quickstart.ps1
```

7 个阶段:
1. **Prereq check**(Python 3.11、Chrome、claude CLI、chatlog/discord 守护)
2. **Python venv** 在 `~/hermes-agent` 安所有 MCP 依赖 + Playwright Chromium
3. **拷文件** 到 `~/Scripts/market-brief`、`~/twitter-mcp`、`~/stock-mcp`
4. **暂停手动**:填 `secrets.json`、改 `prompt.md` 群列表、扫 iLink 微信 QR、导 X cookies
5. **注册 6 个 scheduled task**(MarketBrief / WeixinListener / TwitterMCP / StockPriceMCP / PolymarketMCP / FinancialJuiceMCP)—— 全部通过 `wscript.exe` + `run-hidden.vbs` 启动,**触发时不再闪 PowerShell 窗口**
6. 起守护进程 + `claude mcp add` 注册 HTTP MCPs
7. 可选 smoke test(跑一次 `run.ps1 -SkipEmail`)

随时重跑:每个阶段都会检测"已完成"自动跳过。可用 `-SkipPhase 1,7` 跳指定阶段,`-DryRun` 干跑。

### macOS:一键安装

```bash
git clone https://github.com/ouyadi/market-brief.git ~/market-brief
cd ~/market-brief
bash scripts/macos/quickstart-mac.sh
```

7 阶段跟 Windows 对等,scheduler 改为 launchd。注意 `chat-mcp-setup` skill 的 Mac 路径要先装好(WeChat 4.x macOS 支持还未完全验证,见 SKILL.md macOS 章节)。

### 作为 Claude Code skill(让 Claude 驱动安装)

```powershell
# Windows
git clone https://github.com/ouyadi/market-brief.git $env:USERPROFILE\market-brief
New-Item -ItemType Junction `
    -Path $env:USERPROFILE\.claude\skills\market-brief-setup `
    -Target $env:USERPROFILE\market-brief
# 重启 Claude Code,新会话说:
# "use the market-brief-setup skill to install the hourly chat-intel pipeline"
```

Claude 读 SKILL.md 把 install 步骤一步步走完。手动环节(填群、扫 QR、贴 OAuth token + Gmail App Password)还是要你自己来,其余全自动。

### 完全手动

读 [SETUP-GUIDE.md](SETUP-GUIDE.md) —— 跟 skill 一样的内容,只是给人类操作员的线性版本。

## 前置条件

- **Windows 10/11 或 macOS** 桌面,在目标时段开机不关(scheduler 关机时不会触发)
- **chatlog + discord-selfbot MCP servers** 跑在 `127.0.0.1:5030` / `127.0.0.1:6280` —— 安装脚本在本仓 [`chat-mcp-setup/`](chat-mcp-setup/)。读 [`chat-mcp-setup/SKILL.md`](chat-mcp-setup/SKILL.md) 按平台跑 install
- **微信 4.x** 登录在同一台机器上 chatlog 才能读 DB
- **Claude Code CLI** (`npm install -g @anthropic-ai/claude-code`) + 长期 OAuth token (`claude setup-token`)
- **Gmail 应用专用密码**(Outlook/Hotmail 不行,微软关了 basic auth)
- *(可选,启用微信 push 时)* 用手机扫一次 iLink QR

## 装完后机器上会有什么

| 路径 | 用途 |
|---|---|
| `~/Scripts/market-brief/` | 主目录(run / prompt / push_weixin / listen_weixin) |
| `~/hermes-agent/.venv/` | Python 3.11 venv(装 Hermes Agent,启用微信 push 时) |
| `~/.hermes/.env` | iLink 凭证(QR 扫码后生成) |
| `~/Reports/YYYY-MM-DD-HH-brief.md` | 每小时简报存盘 |
| `~/twitter-mcp/` *(可选)* | Twitter MCP 主目录 + cookies `.env` + logs |
| `~/stock-mcp/` *(可选)* | Stock MCP 主目录 + logs |
| `~/polymarket-mcp/` *(可选)* | Polymarket MCP 主目录 + logs |
| `~/financialjuice-mcp/` *(可选)* | FinancialJuice MCP 主目录 + cache.jsonl + logs |
| Scheduled Task `MarketBrief` / launchd `com.ouyadi.market-brief` | 每小时 08:00–22:00 美东触发 |
| `WeixinListener` *(可选)* | 入站监听:微信 ↔ Claude |
| `TwitterMCP` *(可选)* | X 抓取守护(127.0.0.1:3031) |
| `StockPriceMCP` *(可选)* | yfinance 守护(127.0.0.1:3032) |
| `PolymarketMCP` *(可选)* | Polymarket Gamma + CLOB 守护(127.0.0.1:3033) |
| `FinancialJuiceMCP` *(可选)* | FinancialJuice wire 守护(127.0.0.1:3034) + 自动 cache |

## 仓库内容

| 文件 | 角色 |
|---|---|
| [`SKILL.md`](SKILL.md) | 给 Claude Code 的 procedural 指令(双平台) |
| [`SETUP-GUIDE.md`](SETUP-GUIDE.md) | 给人类操作员的线性安装文档 |
| [`config/prompt.template.md`](config/prompt.template.md) | 扫描 prompt 模板 —— 填你的群 |
| [`config/secrets.example.json`](config/secrets.example.json) | `secrets.json` 模板(OAuth + Gmail) |
| **`scripts/windows/`** | Windows 全套(PowerShell + VBS) |
| &nbsp;&nbsp;[`run.ps1`](scripts/windows/run.ps1) | 主 launcher(claude → 微信 push → 邮件兜底) |
| &nbsp;&nbsp;[`schedule-install.ps1`](scripts/windows/schedule-install.ps1) | 注册 `MarketBrief` Task Scheduler 入口 |
| &nbsp;&nbsp;[`run-listener.ps1`](scripts/windows/run-listener.ps1) / [`install-listener.ps1`](scripts/windows/install-listener.ps1) | 微信 listener 启动 + 注册 |
| &nbsp;&nbsp;[`install-twitter-mcp.ps1`](scripts/windows/install-twitter-mcp.ps1) / [`install-stock-mcp.ps1`](scripts/windows/install-stock-mcp.ps1) / [`install-polymarket-mcp.ps1`](scripts/windows/install-polymarket-mcp.ps1) / [`install-financialjuice-mcp.ps1`](scripts/windows/install-financialjuice-mcp.ps1) | MCP 守护任务注册 |
| &nbsp;&nbsp;[`run-hidden.vbs`](scripts/windows/run-hidden.vbs) | 无窗口启动 wrapper(Task Scheduler 触发不闪 PowerShell) |
| &nbsp;&nbsp;[`hermes-py.ps1`](scripts/windows/hermes-py.ps1) | 早期遗留 wrapper(应急兜底) |
| &nbsp;&nbsp;[`quickstart.ps1`](scripts/windows/quickstart.ps1) | **Windows 一键安装器** —— 7 个幂等阶段 |
| **`scripts/macos/`** | macOS 全套(bash + launchd) |
| &nbsp;&nbsp;[`run.sh`](scripts/macos/run.sh) | 主 launcher(`run.ps1` 的对等版) |
| &nbsp;&nbsp;[`launchd/*.plist`](scripts/macos/launchd/) | 6 个 macOS 守护 plist(market-brief / weixin-listener / twitter-mcp / stock-mcp / polymarket-mcp / financialjuice-mcp) |
| &nbsp;&nbsp;[`quickstart-mac.sh`](scripts/macos/quickstart-mac.sh) | **macOS 一键安装器** —— 7 个幂等阶段 |
| **`mcp/`** | Python MCP servers + 工具(跨平台共用) |
| &nbsp;&nbsp;[`push_weixin.py`](mcp/push_weixin.py) | 微信发送器,支持 `--section` 多 H2 推 + smart 切分 |
| &nbsp;&nbsp;[`qr_login_bootstrap.py`](mcp/qr_login_bootstrap.py) | 一次性 iLink QR 绑定 |
| &nbsp;&nbsp;[`listen_weixin.py`](mcp/listen_weixin.py) | 入站长轮询 listener(微信 → claude → 回复)+ typing keepalive |
| &nbsp;&nbsp;[`twitter_playwright_mcp.py`](mcp/twitter_playwright_mcp.py) | HTTP MCP:Playwright + Chrome + cookies → X DOM(只读,含 thread + 长推 + 图片探测) |
| &nbsp;&nbsp;[`stock_price_mcp.py`](mcp/stock_price_mcp.py) | HTTP MCP:yfinance → quote/history/info/check_post_hoc + 期权链 + BS Greeks |
| &nbsp;&nbsp;[`polymarket_mcp.py`](mcp/polymarket_mcp.py) | HTTP MCP:Polymarket Gamma 发现 + CLOB 时序(9 工具,含 prob_change / compute_vol / short_movers) |
| &nbsp;&nbsp;[`financialjuice_mcp.py`](mcp/financialjuice_mcp.py) | HTTP MCP:financialjuice.com RSS wire,auto-tag + local cache.jsonl |

装完后,`secrets.json` 和你个性化过的 `prompt.md` / `memory.md`(含真实群 ID + KOL 角度 + 你的运行经验)在本机存在但**全部 gitignored** —— **绝对不要 commit**。

## Memory + 自我进化(closed-loop learning)

简报不是静态 prompt 跑模板。系统通过 `memory.md` + 5 个 meta-工具持续累积**用户角度** + **运行经验**:

```
~/Scripts/market-brief/memory.md   ←─ listener / 你手动追加 / /reflect 提议
       ↓ prepend (每次定时简报跑前)
prompt.md (scan / 时段 / 输出格式)
       ↓ pipe to claude
brief 出来时已经知道:
  - Cramer = 反指(不是顺指)
  - SKM = Anthropic equity proxy(不是韩国电信防御票)
  - imnotharsh = INTC bull thesis 核心 voice
  - embed-heavy Discord 频道用 limit=15 起步(不是 80)
  - ...
```

5 个 self-evolution slash commands(微信触发):
- **`/critique [N]`** — meta-review 最近简报 vs raw data 找 gap,提议 prompt 改动
- **`/kol_drift`** — 检测大 V 主战场漂移,提议更新描述
- **`/heat [N]`** — 从最近 N 份简报抽未在 watchlist 的高频 ticker
- **`/score [Nd] [hzn]`** — ⚡ setup 命中率统计
- **`/reflect [Nd]`** — 自我检讨 ≥2 次重复出现的运行经验,沉淀到 memory.md(正向 / 负向 / 淘汰)

Phase 2a scheduler 会在工作日晚 23:10 自动轮转跑一个 meta 命令,只生成待审 proposal,**绝不自动 apply**:

| Day | Command |
|---|---|
| Mon | `/score 14d 3d` |
| Tue | `/heat 8` |
| Wed | `/critique` |
| Thu | `/kol_drift` |
| Fri | `/reflect 7d` |

每个 slash 输出存 `~/Scripts/market-brief/last_<cmd>.md`,让后续"按上次建议加"这种 follow-up 能跨消息引用上次内容(每条微信消息是 fresh `claude --print` session,无内置记忆)。

proposal 审核命令:
- **`/proposals [Nd]`** — 列待审 proposal
- **`/show <id>`** — 看完整内容 + diff preview
- **`/apply <id>`** — 只对白名单 section 自动落盘
- **`/reject <id>`** — 丢弃
- **`/rollback memory.md|prompt.md`** — 回滚最近一次 apply

**memory.md 5 节**(模板见 [`config/memory.template.md`](config/memory.template.md)):
1. 信号优先级金字塔(期权市场定价 > Polymarket > 大V 跨源 > 群里多次 > 单条)
2. KOL 真实角度表
3. Watchlist 真实跟踪角度表
4. 群组/频道说明
5. **运行经验:正向**(好做法)+ **运行经验:负向**(避坑) — 由 `/reflect` 沉淀

## 变体

- **只邮箱模式**:不想 push 微信,quickstart 自动跳过 Step 4–6 (Hermes 安装 + QR + push smoke)。pipeline 自动降级成 `claude → 邮箱`。
- **macOS**:跟 Windows 架构对等,完整支持。launchd 替代 Task Scheduler,bash 替代 PowerShell。详见 SKILL.md 末尾 macOS 章节。
- **Linux**:未测,Hermes Agent 原生支持 Linux,把 launchd plist 改成 systemd timer 应该能跑。

## License

[MIT](LICENSE) —— 自由 fork / 改 / 分发。署名感谢但不强制。

## 已知坑(避雷指南)

完整故障排查表在 [SKILL.md](SKILL.md#troubleshooting)。核心几条:

- **Windows 上别用 uv 管理的 Python 装 Hermes Agent** —— Defender 实测会隔离 astral-sh 的 Python 分发版,venv 一启动就报 `No Python at …`。改用 `winget install Python.Python.3.11`。SKILL.md / SETUP-GUIDE.md 详细写了。
- **Pillow 必装,Hermes Agent 不会自动拉** —— 否则 QR 扫码步骤崩 `ModuleNotFoundError: PIL`。venv 里 `pip install Pillow`。
- **Discord 图片 OCR 依赖两层都要装** —— 系统层 `winget install --id tesseract-ocr.tesseract --source winget`,venv 层 `pip install pytesseract`。中文截图还需要 `~/Scripts/market-brief/tessdata/chi_sim.traineddata`;worker 会优先用这个本地 tessdata 目录,避免计划任务拿不到系统 PATH。
- **iLink session quota**:历史上每 session ~10 条 outbound 后耗尽,需要人扫码或在微信回 bot 一句。**Hermes 已自动 tokenless retry**(`ret=-14` 触发后透明地降级 send,实测 30+ 天 0 失败),加上本仓 listener 的 typing-ping keepalive,日常 100% 通过率,你基本不用管。
- **iLink token 长期过期**(几个月一次):重跑 `qr_login_bootstrap.py` 扫码,无法自动化。
- **chatlog 是 TUI 应用**,只能 minimized 不能完全 hidden(没 console 它直接 crash)。其他所有任务都已通过 `wscript+VBS` 改成零窗口闪烁。
