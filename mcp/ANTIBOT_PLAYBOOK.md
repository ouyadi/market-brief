# 抓取反检测 Playbook(跨 MCP 共享)

> 所有"抓公开站点"的 MCP(twitter Playwright、seekingalpha-premium、wsj、tipranks、未来新增的)共用这套经验。来源:2026-06 给 alphalens tier-2 KOL 抓取做的反爬加固。**改任何 scraping MCP 前先读这页。**

---

## 0. 第一原则:分清两层,别混

| 层 | 是什么 | 能不能做得像人 |
|---|---|---|
| **① 单次请求 / 会话指纹** | 这一个请求看起来是不是真浏览器/真登录用户 | ✅ **能**,见 §1 |
| **② 聚合行为** | 一段时间内的访问"模式"像不像人 | ⚠️ **本质不能**——"每小时抓 40 个 profile、7×24"再美的指纹也不像人;只能靠 **量 / 昼夜节律 / 账号隔离** 压(§2、§3) |

> 最常见的错误是只猛搞 ① 而忽略 ②。被封几乎都是栽在 ②(量大、规律、不睡觉)。

---

## 1. 第 ① 层:让单次请求像真浏览器(checklist)

- **真浏览器,别用 HTTP 爬虫库**:Playwright `channel="chrome"`(系统真 Chrome,不是 bundled chromium)。逆向 GraphQL 的 twscrape/agent-twitter-client 早被灭。
  - HTTP 类(seekingalpha/wsj/tipranks 用 curl_cffi):靠 **TLS/JA3 指纹伪装成 Chrome**(`impersonate`)。curl_cffi 干这个,普通 requests 不行。**进阶(tipranks 已做)**:从**指纹池**每次运行挑一个(`chrome142/136/131`——一次运行内一致、跨天轮换);**持久化 cookie jar**(`__cf_bm`/`cf_clearance`)跨运行保连续性;首次先 **GET 首页 warm-up** 像真导航那样拿 `__cf_bm` 再打 API。
- **真 cookie 登录态**:登录用户被宽容得多。cookie 会过期(尤其 csrf/ct0 轮换)→ 做 **新鲜度看护**,别让过期 cookie 触发重试风暴。
- **住宅 IP**:在桌面跑;OCI 需要时经 CF tunnel **回调桌面**,让出口仍是住宅 IP,不是机房 IP(机房 IP 是强信号)。
- **只读**:绝不暴露 like/RT/follow/post/发帖。最低封号面。
- **UA 必须跟真实浏览器版本**:硬编码 UA 一旦落后真实 Chrome(实测 131 vs 148)→ `navigator.userAgent` 与 `Sec-CH-UA`/`userAgentData`/JS 引擎**不一致 = headless 实锤**。
  - 最佳:**headful 不覆盖 UA**(真 Chrome 自带正确 UA + UA-CH,且无 `HeadlessChrome` token)。
  - 退而求其次(headless):从 `browser.version` 动态构造 UA。
- **时区一致**:headless 默认 UTC,与美国住宅 IP 矛盾 → `timezone_id="America/New_York"`。
- **保守 stealth init_script**:`navigator.webdriver=undefined`、`languages`、`chrome.runtime`、`permissions.query`。**刻意不碰 WebGL/plugins**——伪造的 GPU 串和真硬件不一致比不伪造更糟。
- **headful > headless**(有桌面会话时):真 WebGL/GPU/plugins,一次消灭整类"是不是 headless"检测。窗口推屏外(`--window-position=-32000,-32000`)。**前提**:任务跑在交互会话(session 1),不是 session 0 服务——后者无显示器,headful 启动失败 → 必须带**降级回 headless**。
- **持久化 profile**(`launch_persistent_context`,**专用目录**,别用用户真 Chrome 的 profile 避免单实例锁冲突):累积 cookie/localStorage/cache/history → 像"回头用户"而非每次全新隐身窗。
- **轮换 viewport**(别永远同一尺寸)+ **随机化 recycle 阈值**(别周期性重启 = 可指纹)。

## 2. 第 ② 层:行为别像机器人(更重要)

- **抖动一切**:固定 `sleep(1500)` / 正点 cron 是钟摆信号。请求间隔随机化、cron 加随机偏移/脚本启动抖动。
- **抖动用 log-normal,不用 uniform**(SA/WSJ MCP 已这么做):`random.lognormvariate(mu,sigma)` 夹在 [min,max],模拟人类"看一眼的耗时"分布(多数快、偶尔长尾),比均匀分布更像真人 think-time。
- **双层速率地板(token bucket)**:全局 min-gap + **按 endpoint** min-gap(`/api/metrics` 与 `/api/symbol_data` 各自计时),取 `max(地板, think-time)` 而非相加(地板是硬约束,think-time 已超过就够了)。
- **连 coffee break 的阈值也随机**:每 N 次来一次长歇,但 **N 本身随机**(`N_MIN + rand(0,RANGE)`)——否则"总在第 50 次歇"本身就是指纹。
- **拟人交互**:真实 `mouse.wheel` 事件(非 `window.scrollBy` 瞬跳)、变距/变停顿、滚动前移鼠标到内容区、落地先 dwell"读首屏"、偶尔向上回滚"重读"。
- **限流自愈 + 厂商专属 challenge 检测 → 熔断器**(SA=PerimeterX、WSJ=DataDome、tipranks=Cloudflare 各有专属标记):响应后 hook 查 header(`Server: PX/DataDome`、`x-datadome-cid`、`_pxhd` set-cookie)+ 403/429 body 标记(`perimeterx`/`px-cloud.net`/`firstpartyenabled`/`datadome`/`captcha-delivery.com`/challenge HTML)→ 命中即**开熔断**(冷却 N 秒,期间所有请求 fail-fast,别傻撞进风控)。另设连败计数阈值兜底。注意 PX/DataDome 会在 **API 端点返回首方 block JSON**(非传统 challenge 页),body 标记要覆盖到。
- **昼夜节律**(单条最像人的信号):夜里安静(人会睡觉)。重活只在清醒时段跑(对齐 market-brief 的 08-22 EDT)。**7×24 平直活动是头号机器人特征。**
- **量的纪律**:小批量、摊开。大集合用**滚动游标轮转**(每轮 N 个,慢慢扫完再循环),把每个时间窗速率压到"已验证安全的基线"量级。
- **访问顺序别有结构**:别按字母/ID 顺序遍历 profile(@aaa,@aab… 是爬虫模式)→ 用 hash 打散成确定但无序。
- **硬天花板**:高频访问(如 40 profile/小时)本质不像人,指纹层盖不掉 → 见 §3。

## 3. 账号隔离(后果缓解,§2 天花板的真正解法)

单账号 = 单点失败 + 它往往是**所有抓取的命脉**(market-brief、个股页、一切都靠这组 cookie)。因为第 ② 层本质盖不掉,真正的保护是:**万一被标记,塌的不是主号。**

- 高频/高风险车道(如批量 backfill)→ 用**小号**承载。
- 搜索 / 直接访问 profile 的车道**不需要小号关注任何人**,搭起来很简单(给小号 cookie 即可)。
- 主号只留低频/交互/高价值车道。

## 4. 他们到底查什么(检测向量速查)

`navigator.webdriver` · headless UA token(`HeadlessChrome`)· WebGL=SwiftShader(软件渲染)· 空 `navigator.plugins` · 缺 `chrome.runtime` · UTC 时区 vs 非 UTC IP · UA 与 Sec-CH-UA 版本不一致 · 机房 IP · 请求速率/速度 · 钟摆式定时 · 7×24 无间断 · 写操作 · 顺序化访问模式 · TLS/JA3 指纹(HTTP 爬虫)。

---

## 5. 本栈现状(谁做了什么)

| MCP | 类型 | 已应用 |
|---|---|---|
| **twitter**(`~/twitter-mcp/twitter_playwright_mcp.py`) | Playwright 真浏览器 | ① 全套:headful+persistent、动态 UA、stealth init、tz、viewport/recycle 轮换;② 拟人滚动(**log-normal think-time 停顿**,2026-06-06 从 SA/WSJ 横移)、限流 stand-down。任务跑 Interactive 会话(可 headful) |
| **seekingalpha-premium / wsj / tipranks** | curl_cffi HTTP + cookie | ✅ **已全套硬核,反而是本栈的参考实现**(2026-06-06 核查):log-normal think-time + 双层 token-bucket + 随机阈值 coffee break + 厂商专属 challenge 熔断(PX/DataDome/CF)+ cookie 持久化 + warm-up + impersonate 指纹池(跨天轮换)。tipranks scan 已 `random.shuffle` 顺序 + 定时 07:45/16:45 ET(本就清醒时段,2x/天)。**无待补**——昼夜节律对"按需工具 + 2x/天扫描"不适用;改新 scraping MCP 直接抄它们 |
| 复用工具 | — | TS 限流/抖动:`alphalens/src/lib/mcp/throttle.ts` = ThrottleGuard + sleepJitter(cron 去钟摆)+ **sleepThink/thinkTimeMs(log-normal think-time)+ CoffeeBreak(随机阈值长歇)**,后两者 2026-06-06 从 SA/WSJ 横移;ingest-twitter/cashtag/backfill 逐请求用 sleepThink,backfill/ingest-twitter 用 CoffeeBreak。Python 端滴灌范式:`~/twitter-mcp/_resolve_ids.py`(连败→冷却→回收→续跑) |

## 6. 给"新 scraping MCP"的最小清单

1. 真浏览器(channel=chrome,优先 headful+persistent+降级)或 curl_cffi(HTTP)
2. 真 cookie + 新鲜度看护;只读
3. 住宅 IP 出口
4. 动态 UA / 不覆盖 UA(headful);tz 与 IP 一致
5. 抖动 + 限流 stand-down + 断点续跑
6. 昼夜节律(夜里停)+ 小批量滚动
7. 高频车道上小号隔离主号
8. 顺序打散
