# Market-Brief Setup Guide

> 一份完整的、可重现的安装指南。目标:在一台 Windows 桌面上每小时扫 Discord + WeChat 股票群、用 Claude Code 出一份中文简报、自动推到自己的微信、失败时邮件 fallback。
>
> 全部组件本地运行,不依赖任何 LAN gateway 或云服务(除了 Anthropic API 和 SMTP)。

---

## 这个东西是干啥的

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Windows Task Scheduler "MarketBrief"                                     │
│   ↓ 每小时 fire 一次 (08:00-22:00 EDT, 15 次/天)                          │
│                                                                          │
│ run.ps1                                                                  │
│   ↓ 设环境变量、剥掉污染的 CLAUDE_* env、加载 secrets                       │
│                                                                          │
│ claude --print --dangerously-skip-permissions (Claude Code CLI)          │
│   ↓ 读 prompt.md, 用 MCP 工具调用以下两个 server:                          │
│                                                                          │
│   ┌──────────────────────────────┐  ┌────────────────────────────────┐   │
│   │ chatlog @ 127.0.0.1:5030/mcp │  │ discord-selfbot @ :6280/mcp    │   │
│   │ ouyadi/chatlog_alpha fork    │  │ Python aiohttp + discord.py    │   │
│   │ (Go binary, reads encrypted  │  │ (long-poll user-token bot)     │   │
│   │  WeChat 4.x SQLite DBs)      │  │                                │   │
│   └──────────────────────────────┘  └────────────────────────────────┘   │
│   ↓ 写 markdown 报告到 C:\Users\<u>\Reports\YYYY-MM-DD-HH-brief.md        │
│                                                                          │
│ push_weixin.py (调用 Hermes Agent 的 gateway.platforms.weixin)            │
│   ↓ 自动按 2000 字切片, POST 到 https://ilinkai.weixin.qq.com             │
│   ↓ 你的微信里 bot 收到 3-5 段连续消息                                       │
│                                                                          │
│ [失败时] Send-MailMessage → Gmail SMTP → 邮箱备份                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 前置条件

| 条件 | 备注 |
|---|---|
| Windows 10/11 桌面 | 一直开机(Task Scheduler 不会在关机时触发) |
| WeChat 4.x 已登录目标账号 | 注意 4.x;3.x 的 SQLite 加密 schema 不同 |
| Discord 账号(可加群、有自己的 user token) | self-bot 是用 user token,**不是**官方 bot token |
| Gmail 账号 | 用于 fallback 邮件;Outlook 不行(微软关了 basic auth) |
| Anthropic 订阅 + Claude Code | 走 OAuth subscription,不直接计费 API token |

---

## Step 1 — Claude Code (npm CLI + 长期 OAuth token)

桌面版 Claude Code 跟 npm CLI 的认证**分开**。无人值守模式必须用 npm CLI + 一个长期 OAuth token。

```powershell
# 1. Install Node.js LTS 如果没有: winget install OpenJS.NodeJS.LTS
# 2. Install claude
npm install -g @anthropic-ai/claude-code

# 3. 第一次跑会要求登录,弹浏览器
claude

# 4. 关键: 生成一个长期 OAuth token (1 年有效),用于无人值守
claude setup-token
# 会打印一个 sk-ant-oat01-... 之类的 token, 复制下来, 后面 secrets.json 里要用
```

> **陷阱**: Claude Desktop 会往用户 env 里注入 `ANTHROPIC_BASE_URL` 之类的 host-managed shim 变量。如果这些泄漏进 npm CLI 的子进程,会触发 opaque `API Error: 405`。后面的 `run.ps1` 防御性地剥掉一长串这类变量。

---

## Step 2 — chatlog (WeChat MCP server)

使用 **ouyadi/chatlog_alpha** 这个 fork,带两个关键 patch:
- MCP wrapper timeout 30s → 300s ([upstream PR #64](https://github.com/teest114514/chatlog_alpha/pull/64))
- per-talker fetch floor 1000 → 20 when no sender/keyword filter ([upstream PR #65](https://github.com/teest114514/chatlog_alpha/pull/65))

没有这两个 patch, `wx_history` 每次都会在 30s 超时挂掉。

### 选项 A:用预编译 binary

```powershell
New-Item -ItemType Directory -Force -Path "C:\Users\$env:USERNAME\chatlog"
# 从 https://github.com/teest114514/chatlog_alpha/releases 拿最新 windows-amd64.exe
# (注意: 上游的没有我们的 patch, 想要稳定 morning-brief 工作流, 见选项 B)
```

### 选项 B(推荐):从 fork 编译

```powershell
# CGO 必需; Defender 不会动 winget 装的 Go + MinGW
winget install GoLang.Go
winget install BrechtSanders.WinLibs.MCF.UCRT
# 新开一个 shell 让 PATH 刷新
```

```bash
git clone git@github.com:ouyadi/chatlog_alpha.git ~/Projects/chatlog_alpha
cd ~/Projects/chatlog_alpha
git checkout profile-and-tune

# 关键: CGO=1, 否则 sqlite3 是 stub, 历史全空
export PATH="/c/Users/$USER/AppData/Local/Microsoft/WinGet/Packages/BrechtSanders.WinLibs.MCF.UCRT_Microsoft.Winget.Source_8wekyb3d8bbwe/mingw64/bin:$PATH"
export CGO_ENABLED=1
"/c/Program Files/Go/bin/go.exe" build -o chatlog.exe .

# 预期 ~72MB; 47MB 说明 CGO=0 了, 别部署
cp ./chatlog.exe /c/Users/$USER/chatlog/chatlog.exe
```

### 初次启动 + 提取 data key

```powershell
cd C:\Users\$env:USERNAME\chatlog
.\chatlog.exe
```

在 TUI 里用 ↑/↓ Enter:
1. **Add account** — 自动找到 `%USERPROFILE%\OneDrive\Documents\xwechat_files\<account>` 或 `%USERPROFILE%\Documents\WeChat Files\<account>`
2. **获取数据 Key** — 从运行中的 WeChat 进程抽 SQLite 加密 key
3. **开启 HTTP 服务** — 监听 `127.0.0.1:5030`

验证:
```powershell
Invoke-RestMethod http://127.0.0.1:5030/health   # → @{status=ok}
```

### chatlog.json 推荐配置

`%USERPROFILE%\.chatlog\chatlog.json`:
```json
{
  "history": [{
    "auto_decrypt_debounce": 1800,
    "wal_enabled": false,
    "http_enabled": true,
    "http_addr": "127.0.0.1:5030"
  }]
}
```
`auto_decrypt_debounce: 1800` 防止每次 WeChat 写都触发 re-decrypt(默认 0 会拖垮 `/history` 查询)。

### Daemon-ize via Task Scheduler

chatlog 没 headless 模式,TUI 进程就是 server。用 Task Scheduler "At Log On + window=Minimized" 让它后台跑。脚本在 chat-mcp-setup skill 里(`host/windows/scripts/install-mcp-services.ps1`)。

可选:每天凌晨 3:00 + 中午 12:30 重启一次 chatlog,避免 goroutine 累积。

---

## Step 3 — discord-selfbot MCP

```powershell
$dst = "C:\Users\$env:USERNAME\discord-selfbot-mcp"
New-Item -ItemType Directory -Force -Path $dst | Out-Null

# 从 chat-mcp-setup skill 复制 server.py + pyproject.toml
# (具体参看 mcp-chat-skills 仓库)

# token 放在 $env:USERPROFILE\.discord-selfbot.env
# 内容:
#   DISCORD_USER_TOKEN=mfa.xxxx...
# 锁权限:
icacls "$env:USERPROFILE\.discord-selfbot.env" /inheritance:r /grant:r "$env:USERNAME:(R,W)"

# uv 装 venv (uv venv 下载 Python 3.10+)
cd $dst
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -e .
```

> **拿 Discord user token**:浏览器打开 Discord → F12 → Network → 任一请求看 `Authorization` header。**这是 user token, 共享 = 账号丢**。

Daemon-ize 同上,用 "At Log On + window=Hidden"。

---

## Step 4 — 注册 MCP servers 到 claude

```powershell
claude mcp add --transport http --scope user chatlog         http://127.0.0.1:5030/mcp
claude mcp add --transport http --scope user discord-selfbot http://127.0.0.1:6280/mcp
claude mcp list   # 两个都应该 ✓ Connected
```

---

## Step 5 — market-brief 目录 + Task Scheduler

把当前 `C:\Users\ouyad\Scripts\market-brief\` 整个目录复制到你的对应位置。需要改的:

| 文件 | 改什么 |
|---|---|
| `prompt.md` | Discord channel_id 和 WeChat chatroom_id 改成你加的群;输出文件名格式可以保留 |
| `secrets.json` | 用 `secrets.example.json` 当模板,填 OAuth token + Gmail 信息 |
| `schedule-install.ps1` | 时间/时区/频率按需调 |
| `run.ps1` | 一般不动,但里面 EDT 偏移如果你时区不同要改(line `AddHours(-4)`) |

```powershell
# 注册任务
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\Users\<你>\Scripts\market-brief\schedule-install.ps1

# 立刻触发一次测试 (不发邮件)
& 'C:\Users\<你>\Scripts\market-brief\run.ps1' -SkipEmail
```

`secrets.json` 模板:
```json
{
  "claudeCodeOauthToken": "sk-ant-oat01-...",
  "smtpServer": "smtp.gmail.com",
  "smtpPort": 587,
  "smtpUser": "你@gmail.com",
  "smtpPassword": "abcd efgh ijkl mnop",
  "fromAddress": "你@gmail.com",
  "toAddress": "你@gmail.com"
}
```

锁权限:
```powershell
icacls C:\Users\<你>\Scripts\market-brief\secrets.json /inheritance:r /grant:r "$env:USERNAME:(R,W)"
```

---

## Step 6 — Hermes Agent + WeChat iLink push

这是把简报送到自己微信的关键。底层走 iLink 协议(腾讯的内部 API),Hermes Agent 提供 Python adapter。

### 6.1 装 Python 3.11 + venv

**不要**用 uv 的 managed Python — Defender 会把 astral-sh 的 unsigned distribution 当 PUA 隔离(踩过)。改用 winget 装签名版:

```powershell
winget install Python.Python.3.11
# 装在 %LOCALAPPDATA%\Programs\Python\Python311\

$sysPy = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
& $sysPy -m venv C:\Users\$env:USERNAME\hermes-agent\.venv

$venvPy = "C:\Users\$env:USERNAME\hermes-agent\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install hermes-agent qrcode aiohttp cryptography Pillow
# 注意 Pillow 必装 — hermes-agent 不会带, 但 qrcode.make() 默认要 PIL
```

> **不需要** `hermes-agent[messaging]` 这个 extra — 它会拉 Discord/Telegram/Slack 一堆。core 包就够,weixin adapter 在核心里。

### 6.2 用 QR 扫码绑定 iLink

把 `qr_login_bootstrap.py` 复制到 market-brief 目录(已经包含),然后:

```powershell
& 'C:\Users\<你>\hermes-agent\.venv\Scripts\python.exe' `
    'C:\Users\<你>\Scripts\market-brief\qr_login_bootstrap.py'
```

会:
1. 把二维码渲染成 PNG 存到 `%TEMP%\hermes-qr.png`
2. 自动用「照片」应用弹出
3. 你**手机微信扫桌面屏幕**(不是扫电脑里的 WeChat),手机上点确认
4. 凭证落到 `~/.hermes/.env`:
   ```
   WEIXIN_ACCOUNT_ID=xxxxx@im.bot       # iLink bot 自己 ID (发送方)
   WEIXIN_TOKEN=<64-char>               # iLink session token
   WEIXIN_BASE_URL=https://ilinkai.weixin.qq.com
   WEIXIN_HOME_CHANNEL=xxxxx@im.wechat  # 你自己 WeChat ID (接收方)
   ```

### 6.3 测试推送

```powershell
& "$venvPy" 'C:\Users\<你>\Scripts\market-brief\push_weixin.py' --message "test"
```

微信端应该收到这条 "test" 消息。如果收到,说明 iLink ↔ Hermes ↔ Python 这条路通了。

### 6.4 iLink session 模型(必须理解)

iLink 服务端维护一个"会话窗口":

```
        ┌─→ 用户在微信里主动给那个 bot 发任何一句话
        │   = iLink 给该用户 ↔ bot 之间开/续 session
        │
        ▼
session 开启后, bot 拿 context_token 可以连续主动推 ~10 条
        │
        ▼
        10 条用完, 或长时间无交互
        │
        ▼
        服务端返回 ret=-14 (session expired)
        │
        ▼
   要么用户再给 bot 发一句话续 session, 要么重新扫码拿新 token
```

`send_weixin_direct` 在内部:遇到 -14 会自动清掉本地 context_token 再重试一次(等于"先没 context 重发让服务端给个新的")。一般一次重试能成。

**运维信号**: 如果某天你的 fallback 邮件突然开始按小时进 inbox,大概率是 iLink session 卡死。去那个 bot 那里回一句话(任何字)就续上;还不行就重跑 `qr_login_bootstrap.py`。

---

## Step 7 — Gmail App Password (fallback 邮件)

1. 启用 Google 账号的两步验证: https://myaccount.google.com/security
2. 生成 App Password: https://myaccount.google.com/apppasswords
3. 拿到 16 个字符密码(形如 `abcd efgh ijkl mnop`),填到 `secrets.json` 的 `smtpPassword`

**不要用真的 Gmail 密码**。也**不要用 Outlook/Hotmail** — 微软关了 basic auth,只能走 OAuth2,Send-MailMessage 不支持。

---

## 完整 run.ps1 行为(已经在 `run.ps1` 里)

```
1. EDT 时间 + 30s 偏移 (避免时钟漂移导致 hour boundary 误判)
2. 根据 hour 决定 session tag (pre-market / market / after-hours)
3. 剥掉所有 Claude Desktop 注入的 env var
4. 加载 secrets.json, 设 CLAUDE_CODE_OAUTH_TOKEN
5. 跑 claude --print --dangerously-skip-permissions, 走 prompt.md
6. 验证报告文件存在
7. 推 WeChat: push_weixin.py <报告路径>
   - 成功 → exit 0, 不发邮件
   - 失败 → 走 fallback 邮件
8. (fallback) Send-MailMessage subject 加 [fallback] 标记
```

---

## 维护 cheat sheet

```powershell
# 立刻触发一次
Start-ScheduledTask -TaskName MarketBrief

# 看上次结果
Get-ScheduledTaskInfo -TaskName MarketBrief

# 看今天的 log
Get-Content "C:\Users\<你>\Scripts\market-brief\logs\$(Get-Date -Format yyyy-MM-dd).log" -Tail 30

# 看今天最新的报告
Get-Content (Get-ChildItem "C:\Users\<你>\Reports\$(Get-Date -Format yyyy-MM-dd)-*-brief.md" | Sort-Object Name | Select-Object -Last 1)

# 手动跑一次但不发邮件
& 'C:\Users\<你>\Scripts\market-brief\run.ps1' -SkipEmail

# 重新绑微信 (iLink token 过期 / 换设备时)
& 'C:\Users\<你>\hermes-agent\.venv\Scripts\python.exe' `
    'C:\Users\<你>\Scripts\market-brief\qr_login_bootstrap.py'

# 看当前 iLink 凭证 (会显 token)
Get-Content $env:USERPROFILE\.hermes\.env

# 卸载 task
Unregister-ScheduledTask -TaskName MarketBrief -Confirm:$false

# 重启 chatlog (如果 /history 开始变慢)
Get-Process chatlog | Stop-Process -Force
Start-ScheduledTask -TaskName ChatlogServer

# 看 chatlog log
Get-Content C:\Users\<你>\chatlog\chatlog\log\chatlog.log -Tail 20
```

---

## Step 8 — 入站监听 (可选,让你能在微信里跟 Claude 聊天)

到此为止 outbound 已经完成(Claude → 你的微信)。如果你还想让 inbound 也通(**你给 bot 发消息 → Claude 用同样的 MCP 工具帮你回答 → 推回你的微信**),装入站 listener。

架构:

```
你在微信里发任何消息给 bot
     ↓
WeixinListener (At log on 启动的隐藏 daemon)
     ↓
listen_weixin.py long-poll iLink /getupdates
     ↓
  斜杠命令(零 Claude 消耗):
    /ping   listener 在线测试
    /brief  立刻触发一次 MarketBrief
    /help   命令列表
  其他文本 → claude --print 带"股票情报员"人设 + 所有 MCP 工具
     ↓
send_weixin_direct 回推到同一个聊天
```

只回复来自 `WEIXIN_HOME_CHANNEL`(也就是你自己)的消息,陌生人 DM 这个 bot 不会被响应。

### 8.1 安装 + 启动

```powershell
# 注册 At-log-on 隐藏窗口的 task
powershell -NoProfile -ExecutionPolicy Bypass `
    -File C:\Users\<你>\Scripts\market-brief\install-listener.ps1

# 立刻启动(不必等下次登录)
Start-ScheduledTask -TaskName WeixinListener

# 确认有且仅有一对(venv shim + base interpreter)listener 进程
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'listen_weixin' } |
    Select-Object ProcessId, ParentProcessId, ExecutablePath, CommandLine |
    Format-List
```

### 8.2 测试

1. 在微信发 `/ping` → 应当几秒内收到 `pong (listener up Xh Ym Zs)`
2. 发 `/help` → 收到命令列表
3. 发个真问题(`月哥下午说啥了`)→ 20s-3min 后收到 Claude 的中文回答(带具体出处和数字)

日志位置:
- `C:\Users\<你>\Scripts\market-brief\logs\listen_weixin.log` — listener 自己的结构化日志
- `listener.YYYY-MM-DD.err.log` — PowerShell wrapper 的 stderr(parse 错误会落这里)

### 8.3 注意事项

- **run-listener.ps1 必须纯 ASCII**(无中文、无中划线 `—`、无智能引号)。Task Scheduler spawn `powershell.exe -File <脚本>` 时,没 BOM 的 .ps1 用系统 codepage 解析,非 ASCII 字节会破坏引号配对导致 parse 失败
- **入站 + 出站共享同一个 iLink token**,但走不同 endpoint(`/getupdates` vs `/sendmessage`),不会互抢长连接锁
- **任意时刻只跑一个 listener python 进程对**(shim + base interpreter 是一对,parent-child)。如果看到多对独立进程,先 `Stop-Process` 杀掉残留再重启 task
- **每次自由问答都会跑一次 `claude --print`**,消耗 OAuth subscription 的 token。频繁问可能撞 Claude API rate limit

---

## 踩坑清单

| 症状 | 根因 | 修法 |
|---|---|---|
| `API Error: 405` from claude | Desktop 注入的 `ANTHROPIC_BASE_URL` 之类 env var 污染 | run.ps1 已经 strip 一长串;新出现的也加进去 |
| chatlog `wx_history` 30s 全部 timeout | 上游 MCP wrapper 硬编码 30s 客户端 timeout | 用 ouyadi/chatlog_alpha fork 的 PR #64 patch |
| chatlog `/history` 30-90s 慢 | 上游 per-talker fetch floor 1000 行,小 limit 也吃 | 用 ouyadi/chatlog_alpha fork 的 PR #65 patch |
| chatlog 返回空消息 | CGO=0,sqlite3 是 stub | 重编译 with CGO_ENABLED=1 + MinGW gcc;binary 应该 ~72MB |
| uv-managed Python `Test-Path` 突然 False | Defender 把 astral-sh distribution 当 PUA 隔离 | 用 winget 装签名版 Python,别用 uv-managed |
| `ModuleNotFoundError: PIL` in qr_login | hermes-agent core 不带 Pillow | `pip install Pillow` |
| Gmail SMTP `5.7.139` 拒绝 | Outlook/Hotmail 关了 basic auth | 改用 Gmail + App Password |
| 微信 push 突然每小时都失败 | iLink session 用尽 (~10 quota/session) | 在 WeChat 给那个 bot 回一句话续 session;还不行重扫码 |
| run.ps1 PowerShell 解析失败 | .ps1 里直接写中文字符导致 BOM/UTF-16 误判 | 中文常量用 `[byte[]](0xE7,0xBE,...)` + UTF8.GetString 在内存里拼 |
| Task 在 14:59:58 fire 写到 14 点的文件 | Windows clock skew | run.ps1 已经 `AddSeconds(30)` 加偏移 |
| OneDrive 把 chatlog 数据吃成 cloud-only | Files-On-Demand 默认行为 | `attrib +P` pin 那些目录强制本地 |
| iLink 推到一半 rate limit | Hermes adapter 自己有退避重试,看 log 里 `backing off Ns` 即可 | 一般不用动,Hermes 会自己重试 |
| WeixinListener task 启动了但 `Get-CimInstance` 看不到 python 进程 | `run-listener.ps1` 含非 ASCII 字符 + 无 BOM,task spawn 的 powershell.exe 解析挂掉 | 看 `listener.YYYY-MM-DD.err.log`;把所有非 ASCII 字符(em-dash、中文、智能引号)换成纯 ASCII |
| 微信里发消息给 bot 没反应 | listener 没起 / 多个 listener 进程互相踢 | `Get-CimInstance Win32_Process -Filter "Name='python.exe'" \| ?{ $_.CommandLine -match 'listen_weixin' }` 看进程对数。多个就 Stop-Process 杀掉再 Start-ScheduledTask |

---

## 给只想要邮件不要微信的人

可以全跳过 Step 6 (Hermes/iLink 整个一段)。把 `run.ps1` 里 Step 5 那段(WeChat 推送 + fallback 判断)删了,改回直接 `Send-MailMessage`。架构就变成 claude → email 一条线,极简。

但是邮件 latency 高、邮箱堆积、手机不好搜历史,所以原版的 morning-brief 设计经验是:**邮件作为可靠备份,实时通知走 IM**。

---

## 给 macOS 用户

完全可以做,但 iLink 在 macOS 上同样能跑(Hermes Agent 支持 macOS first-class)。chatlog 也有 macOS binary。但 WeChat 数据 key 提取在 macOS 上路径不同(`~/Library/Containers/com.tencent.xinWeChat/`),自动提取脚本在 chat-mcp-setup skill 的 `host/macos/` 下。整体步骤镜像 Windows 版,跳过 Step 1-5 里所有 "winget" 部分。

---

## 文件清单(完整 setup 结束时应该有的)

```
C:\Users\<你>\
├── chatlog\
│   ├── chatlog.exe              # ouyadi/chatlog_alpha 编译,~72MB
│   └── chatlog\<account>\       # 解密后的 WeChat 数据库
├── discord-selfbot-mcp\
│   ├── .venv\                   # Python venv
│   └── server.py
├── hermes-agent\
│   └── .venv\                   # Python 3.11 venv 装 hermes-agent + Pillow
├── .hermes\
│   ├── .env                     # WEIXIN_TOKEN / WEIXIN_HOME_CHANNEL etc
│   └── weixin\accounts\         # iLink account JSON
├── .discord-selfbot.env         # Discord user token
├── Reports\
│   └── YYYY-MM-DD-HH-brief.md   # 每小时一份报告
└── Scripts\
    └── market-brief\
        ├── prompt.md
        ├── run.ps1
        ├── push_weixin.py
        ├── qr_login_bootstrap.py
        ├── schedule-install.ps1
        ├── listen_weixin.py            # 入站 listener (Step 8)
        ├── run-listener.ps1            # listener wrapper (load secrets + scrub env)
        ├── install-listener.ps1        # 注册 WeixinListener task
        ├── secrets.json
        ├── README.md
        └── logs\
```

Task Scheduler 里应该有这些任务:
- `MarketBrief` - 每小时 08:00-22:00 EDT
- `ChatlogServer` - At Log On
- `DiscordSelfbot` - At Log On
- `WeixinListener` (可选,Step 8) - At Log On,隐藏窗口
- `ChatlogDailyRestart` (可选) - 03:00 EDT
- `ChatlogMidDayRestart` (可选) - 12:30 EDT

---

## 想 share 这份指南

这份 SETUP-GUIDE.md 本身是 self-contained 的(除了引用 chat-mcp-setup skill 的几个外部脚本)。直接把它复制给别人即可。如果对方要从零开始,也参考 [mcp-chat-skills 仓库](https://github.com/teest114514/mcp-chat-skills)(chatlog + discord-selfbot 的安装 skill 在那里)。
