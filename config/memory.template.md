# Persistent memory (read every brief run)

> **What this file is**: cross-run user angles + invariants. `run.sh` / `run.ps1`
> prepends this content to `prompt.md` before piping to Claude. Use it to record
> things that should persist across hourly briefs without manual prompt edits:
> KOL "use as reverse-indicator" notes, watchlist real angles (e.g., "SKM is
> tracked as Anthropic proxy, not a Korean-telecom defensive"), corrections to
> Claude's defaults, etc.
>
> The listener bot (when handling "大 V 加 / 个股加 / 大 V 更新" commands in
> WeChat) should append durable user-stated context here **in addition to**
> editing `prompt.md`'s tables. This is the "memory" half of the system.

## 信号优先级金字塔(简报里 ⚡ section 的判断依据)

多源说法不一致时按权重决断,**别让最响的盖过最重的**:

1. **期权市场实际定价**(implied_move、unusual_activity)— 真金白银 vote
2. **Polymarket 事件概率**— 同上,限于宏观/地缘
3. **大 V 跨源共识**(≥2 个跟踪的大 V 同向 — 注意反指 KOL 要先翻转方向)
4. **群里多次提及 + 实盘校场仓位**
5. **机构研报**(单家)
6. **单个大 V 发言**
7. **单个群里某人的话** — 噪音

⚡ section 的 setup 立场必须站在 1-3 层之上;6-7 层只作 "群氛围" 注脚。

## KOL 真实角度(填你自己 confirm 过的)

`prompt.md` 的"### 大 V X 账号"表里有相同 handle,但**用法 / 反指 / 核心
voice 等真实角度** 应该在这里 — 简明、便于 quick reference。

举例(删掉换成你自己的):

| Handle | 用法 | 关键点 |
|---|---|---|
| `<handle>` | <顺指 / 反指 / 核心 voice for X / 过滤 noise / 其他> | <为什么这么用,关键约束> |

## Watchlist 真实跟踪角度

`prompt.md` 的"### 个股 watchlist" 表里有 ticker,**真实 thesis 角度** 在这里:

举例:

| Ticker | 真实 thesis | 关键 catalyst |
|---|---|---|
| `<TICKER>` | <跟踪角度,可能跟通用描述完全不同> | <主要 catalyst> |

## 群组/频道说明(可选)

特定群/频道的内容规律、jargon、长期 leitmotif 在这里。例:某频道的 bot 把内容
塞在 `message.embeds` 而非 `message.content`;某群的 "实盘校场" 子频道带具体
仓位是钱在动的硬信号。

## 行为约束(给 Claude 自己看)

- **绝不**根据训练数据脑补"X 是 Y 的 proxy" / "Z 是反指"之类 thesis 描述。
  Generic financial framing 听起来对,但经常跟用户真实角度无关。**没明确依据
  就标 `<待验证>`,问用户而不是猜**
- 反指 KOL 的方向要先 flip 再合并到 consensus
- KOL 间 jargon 重叠不是身份标记,别用 jargon 重叠去 attribute 身份
