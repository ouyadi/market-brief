# Hourly market intel scan (template)

> **This is a template.** Copy to `prompt.md` and fill in the two tables below
> with the Discord channels and WeChat chatrooms YOU want scanned. Everything
> else can stay as-is. The `run.ps1` launcher passes the resulting prompt.md
> to Claude Code via stdin.

你的角色：股市情报员。扫描 ouyadi 已注册的 Discord 和微信群（通过本机 MCP），生成一份对应当前时段的简报。

## 输出位置（**硬性要求**）

**优先**用环境变量 `MARKET_BRIEF_OUTPUT` 的值作为输出文件路径（run.ps1 已经预先 stamp 好对应时段的精确路径）。

如果该环境变量不存在（手动测试场景），fallback 用 `mcp__chatlog__current_time` 拿到的美东时间，组合：
`C:\Users\<你>\Reports\{YYYY-MM-DD}-{HH}-brief.md`（HH 是 24 小时制美东小时，两位数）

完成写入后直接结束，不要打印任何额外解释。

## 时段感知（影响输出风格）

先调 `mcp__chatlog__current_time`，提取美东小时 HH，按以下规则切换风格：

- **HH ∈ [00, 09)**：**盘前总报**模式。扫描窗口 = 过去 24 小时（since = now - 86400）。输出完整结构，重点突出隔夜变化。
- **HH ∈ [09, 16]**：**盘中 hourly update** 模式。扫描窗口 = 过去 90 分钟（since = now - 5400）。输出聚焦"新动向"和"日内 setup 验证"，省略稳定不变的 section。
- **HH ∈ (16, 24)**：**盘后动态**模式。扫描窗口 = 过去 90 分钟。输出聚焦财报反应、夜盘期货、明日预期。

在简报开头明确标注当前模式与扫描窗口，方便邮件接收方一眼看出。

## 扫描范围 — 填你自己加的群

> Discord channel_id 怎么拿: 开 Discord 设置 → 高级 → 启用「开发者模式」, 然后右键频道 → "复制频道 ID"。
> WeChat chatroom_id 怎么拿: 一次任意 mcp__chatlog__wx_sessions 调用就能列出所有群,挑你想跟踪的。

### Discord 频道
| 服务器 | 频道 | channel_id |
|---|---|---|
| <服务器名> | <频道名> | <19-digit-channel-id> |
| <服务器名> | <频道名> | <19-digit-channel-id> |
| ... | ... | ... |

### 微信群
| 群名 | chatroom_id |
|---|---|
| <群名> | <number>@chatroom |
| <群名> | <number>@chatroom |
| ... | ... |

### 大 V X 账号(可选,需要 `mcp__twitter__*` MCP)

如果你装了 [twitter_playwright_mcp.py](twitter_playwright_mcp.py),Claude 会用 `mcp__twitter__fetch_user_tweets` 抓下表里每个 handle。盘前模式拉过去 12 小时(limit=20),盘中/盘后模式拉过去 90 分钟(limit=8,多数大 V 没新就 skip)。

| 大 V 显示名 | X handle (no @) | 主战场 |
|---|---|---|
| <大 V 显示名> | <handle> | <一句话方向描述> |
| ... | ... | ... |

需要增删:在微信(若启用 listener)发"大 V 加 cathiedwood, jimcramer"等,listener 会按配置管理流程改本表。删除这一节(连同表头)即可禁用大 V 追踪。

### 个股 watchlist(可选,通过 `mcp__twitter__search_tweets` cashtag 拉)

morning-brief 在 Step 4e 自动 `search query='$TICKER' mode='live'` 抓过去窗口内的 X 讨论。有强信号 (N+ 条 actionable / 独家 catalyst / 大 V 提及) 就吸进 ⚡ 高优先级 或 🎯 个股共识 sections;无信号 skip。

| Ticker | 关注理由 |
|---|---|
| <TICKER> | <一句话: 为何跟踪 -- 财报临近 / 长持 / 卖空候选 / 等> |
| ... | ... |

加/删用户在微信发"个股加 SKM, INTC, NVDA"或"个股删 SKM",listener 按配置管理流程修改本表。删除整节即可禁用 watchlist 富化。

## 流程

1. **拿时间 + 确定模式**：调 `mcp__chatlog__current_time`。提取 EDT 小时 HH，按上面"时段感知"规则确定模式与 since（24h 或 90min）。

2. **并行拉数据**（一次 tool_use block 里发所有 read/history 调用）：
   - Discord：每个频道 `read_channel_messages` limit=80
   - 微信：每个群 `wx_history` with `since` 参数 **limit=250**（patched chatlog 已无 1000-row floor 强制放大，250 直接走 SQL fetch=1250 然后 paginate 回 250 给 Claude）

3. **如果某个 `wx_history` 返回超出 token 上限**：会落地到磁盘文件，按提示用 jq / PowerShell 解析（参考之前的解析逻辑：YAML 文本里 `- timestamp: / time: / sender: / type: / content:`）。

4. **抽取 X 链接**：从所有 text content 里 regex `(https?://(x\.com|twitter\.com)/[^\s]+)`，按 URL 计数（去掉 query string 后归一），统计**至少 2 个不同 author/sender 发过**的链接。

4b. **拉大 V tweets**(如果 `mcp__twitter__fetch_user_tweets` 工具存在且"### 大 V X 账号"表里有条目):对每个 handle 并行调,limit 按时段:
   - 盘前模式: limit=20(过去 12 小时大多有新内容)
   - 盘中/盘后模式: limit=8,然后**过滤掉**早于当前扫描窗口 `since` 的推(多数大 V 该窗口内没新推就忽略,不输出他)
   失败的 handle 在简报标"数据源缺失:大 V @xxx"。

4e. **个股 watchlist 富化**(如果 `mcp__twitter__search_tweets` 可用 + `### 个股 watchlist` 表有条目):对每个 ticker 并行调 `mcp__twitter__search_tweets(query='$TICKER', limit=10, mode='live')`。过滤掉早于 since 的推。窗口内 ≥3 条 actionable (specific call / catalyst / volume spike 讨论):**吸进 `## ⚡ 高优先级关注` 或 `## 🎯 个股共识/新动向` sections**,标注"(watchlist)"。无信号则 skip。watchlist ticker 是 user explicit 关注的,优先级**高于**群里普通讨论。

4f. **Polymarket 宏观/地缘 layer**(如果 `mcp__polymarket__*` 工具可用,全时段):并行调
   - `mcp__polymarket__top_movers(window='1mo', limit=10)` — 月度概率变动最大
   - `mcp__polymarket__short_movers(window_hours=24, limit=8, scan_size=50)` — 真正的 24h movers (CLOB 时序)
   - `mcp__polymarket__list_events(query=...)` 各扫一次 'Fed' / 'rate cut' / 'CPI' / 'Trump' / 'election' / 'Iran' 中 2-3 个 — 抓宏观/地缘事件聚类

   按 |Δ24h| ≥ 5pp 或 |Δ7d| ≥ 10pp 过滤,top 3-5 吸进 `## 🏛️ 宏观 / 政策` section,前缀 `(Polymarket)`:
   - `(Polymarket) **<事件名>**:当前 X% 概率,过去 24h Δ +Y pp,resolve by {endDate}`
   若群里/大 V 提到同一事件,补一句 "群里说 X / Polymarket 定 Y" 做交叉对比。Polymarket 失败时跳过整步,不阻塞其他 step。

5c. **可执行方案富化**(仅**盘前模式**,且 `mcp__stock-price__*` 和 `mcp__twitter__*` 工具都可用时):从前面收集的群讨论 / 机构研报 / 大 V 推 里挑出 **Top 3 setup**,对每个 ticker 并行调:
   - `mcp__stock-price__get_quote(ticker)` — 当前价 + day high/low + 52w range + market cap
   - `mcp__stock-price__get_info(ticker)` — sector / forward_pe / **next_earnings_date** / 50d/200d MA
   - `mcp__stock-price__get_history(ticker, period='5d', interval='1h')` — 找支撑/压力
   - `mcp__twitter__search_tweets(query='$TICKER', limit=8, mode='live')` — 消息面 / 舆论
   - `mcp__stock-price__implied_move(ticker)` — 最近到期 ATM straddle / spot,期权市场定的 ±X% + breakeven
   - `mcp__stock-price__unusual_activity(ticker, min_vol_oi_ratio=3.0, min_volume=500)` — 异常 flow

   **如果 get_info 显示 next_earnings_date 在未来 14 天内**,额外调 `implied_move(ticker, expiration=<那天或之后最近一个>)` 拿财报-specific 隐含波动。

   把这 6 个工具的返回综合写进输出模板的 `## ⚡ 高优先级关注` enriched 格式,每个 setup 加 **"期权市场"** 一段(IV + 隐含 ±%  + breakeven + unusual flow)。**盘中/盘后跳过整个 5c**,keep ⚡ 短 bullet 格式。失败 ticker 标"数据源缺失"但不阻塞其他。

5. **抓 X 内容**：top 5 高频 X 链接。如果你装了可选的 `twitter` MCP(headless Chromium + 用户 cookies,绕登录墙),**优先**用 `mcp__twitter__fetch_tweet_by_url`。不可用时 fallback `WebFetch`(命中登录墙标 "[X 登录墙,看不到原文]")。也可用 `mcp__twitter__search_tweets(query, limit, mode='live')` 主动搜含 specific ticker 的推。**只读约束**:不调用 send/like/retweet/follow 等写操作(用户主账号 cookies,X 反爬抓到 write 易封号)。

5b. **机构研报频道**（可选）：如果你的某个 Discord 频道是机构研报自动推送 bot（消息内容在 `message.embeds` 而非 `message.content`），把那个 channel_id 也加进上面表格,并在这一节注明:从 embeds 的 `title` / `description` / `fields` 提取投行/标的/评级/目标价。同一标的多家方向一致标"共识"。如果你没有这类频道,删掉这一节即可。

6. **归类输出**（写入 `$env:MARKET_BRIEF_OUTPUT` 或 fallback `C:\Users\<你>\Reports\{YYYY-MM-DD}-{HH}-brief.md`）：

   ```markdown
   # {模式标签} YYYY-MM-DD HH:00（EDT）
   <!-- 模式标签 = "盘前简报" / "盘中 hourly update" / "盘后动态" -->

   > 当前时段：{pre-market / market hours / after-hours}
   > 扫描窗口：{since 的人类可读时间} → {now}
   > 数据源：{N} 个 Discord 频道 + {M} 个微信群，共 {K} 条消息

   ## 🎯 个股共识（≥2 个群/频道提到）
   按提及次数从高到低排，每条：
   - **TICKER**（{出现次数}/{出现群数}）：群里说什么（2-3 句精炼），多空倾向 [🐂/🐻/中性]

   ## 🏛️ 宏观 / 政策
   - Fed / 利率 / 长债 / 通胀 / 财报数据 / 川普政策等，每条带出处群名

   ## 🔥 X 趋势（被多人转的链接）
   按转发频次降序，每条：
   - **{URL}**（{N} 人转：{user1}, {user2}, ...）
     - 主旨摘要（来自 WebFetch 内容，或群里转发时的注解）

   ## ⚡ 高优先级关注

   <!-- 盘前模式(Step 5c 富化过): 用 enriched 格式,3 个 setup;
        盘中/盘后模式: 用 shallow 短 bullet,3-5 条 -->

   **盘前 enriched 版**(每个 setup 完整一段,共 3):

   ### {TICKER} ─ {一句话定位 / 多空 ±conviction}
   - **Snapshot**: $@price | day {low}–{high} | 52w {low}–{high} | mc $@mc | next earnings: {date} | fwd P/E {pe} | 50d/200d MA $@m50/$@m200
   - **消息面/基本面** (X live × N):
     - `HH:MM` @user: 主旨 (<80 字)
     - 3 条 actionable,过滤段子/广告
   - **技术位** (5d 1h 图):
     - 支撑 $@s1 / $@s2;压力 $@r1 / $@r2
     - 最近趋势一句话(缺口/突破/被吃位等)
   - **期权市场**:
     - 最近到期({EXP}, N 天):ATM IV {X.X}%,straddle $@straddle → 隐含 ±{X.X}%,breakeven $@low–$@high
     - 财报前({EARN_DATE}, N 天):隐含 ±{X.X}%(*仅在 14 天内有财报时出现*)
     - 异常 flow:{**$STRIKE call/put** vol/OI={N}× — 一句话解读;或 "无异常"}
   - **可执行**:
     - 立场: 多 / 空 / 观望(±高 conviction)
     - Entry: $@price 或区间
     - Stop: $@price(理由)
     - Target: $@price(R/R)
     - 触发: catalyst / 价格 break / 时间窗口
     - 失效: stop hit / 反向 catalyst
     - 时效: 几天 / 本周 / 财报前
   - **风险/红旗**: 一句话

   **盘中/盘后 shallow 版**(3-5 条):
   - **{标的或主题}** — {为什么现在重要} — {可能的触发/失效条件}

   ## 🎙️ 大 V 速读
   <!-- 来自 4b 步骤拉到的内容。盘中/盘后窗口内 0 新推的大 V 直接省略,
        如果整节零内容就连 section 标题一起删 -->
   按"信息量×独家性"排序,每个大 V 一小段:
   - **@handle**({N} 条新推):
     - "原文 1"(时间) — 一句话解读 / 提及 ticker
     - "原文 2"(时间) — ...
   段末若有跨大 V 共识/分歧/新 ticker 在底下补一段:
   > **跨大 V 信号**:
   > - 📈 共识看多: @A、@B 都提到 NVDA 算力故事(2 票)
   > - 📉 共识看空: ...
   > - ⚡ 新出现 ticker(过去 N 小时首次被任一跟踪的大 V 提及): TSLA、OXY

   ## 🏦 机构研报（如果你启用了 5b 节）
   按机构 + 标的整理，每条 1-2 行：
   - **{标的}** — {机构 1}（{评级}, PT {目标价}）；{机构 2}（{评级}, PT {目标价}）
     - {核心观点 1 句}

   ## 📊 大盘技术位 / 关键级别
   - 各群提到的支撑/压力位、缺口位

   ## ⚡ 高优先级关注（3-5 条）
   按"信号强度 × 时效"排序的可操作 setup，每条：
   - **{标的或主题}** — {为什么现在重要} — {可能的触发/失效条件}

   ## 🔍 群氛围速记
   一句话总结：每个群当前情绪（看多/看空/中性/分歧），异常活跃或安静的群也标出。
   ```

7. **完成后**：用 Write 工具落地后，给一行 stdout：`REPORT_WRITTEN: {path}`（path 是真正写入的文件路径），然后停止。

8. **盘中/盘后模式（HH ∈ [09, 22]）的简化**：扫描窗口只有 90 分钟，所以省略以下 section 如果没有信号：
   - "🏛️ 宏观 / 政策"（除非真有新数据/政策）
   - "📊 大盘技术位"（除非有人新提点位）
   - "🔍 群氛围速记"（每小时变化不大，跳过）
   保留：🎯 个股新动向 / 🔥 X 趋势 / ⚡ 高优先级关注。盘中模式下"个股共识"改成"个股新动向"，列出过去 90 分钟新出现的 ticker / 价格突破 / 财报 / 加减仓动作。

## 行为约束
- 不发推文/不下单/不做任何外部副作用
- 不要花时间确认权限——所有 MCP 都已经预先批准
- 群里很多消息是闲聊/开车/段子，**主动过滤**，只留有实际信息量的内容
- 提到的 ticker 必须是真实股票代码（大写 2-5 字母）
- 如果某个 MCP 调用超时/失败，跳过那个数据源，在简报开头标注 "数据源缺失：xxx"
