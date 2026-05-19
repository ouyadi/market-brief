"""selfevolve.py — Phase 2b/2c/2d closed-loop infrastructure.

This module provides the JSON schema, extraction helpers, and proposal
storage layer that connects:

  1. Slash commands (/reflect, /critique, /score, /kol_drift, /heat) which
     now emit a `<proposals>...JSON...</proposals>` block alongside the
     human-readable markdown.
  2. Applier (Phase 2c+2d, separate module) which reads from
     ~/Scripts/market-brief/selfevolve/proposals/ and lets the user
     approve/reject patches via WeChat slash commands.

Design notes:

  - **Scope-lock** (CRITICAL). The applier MUST refuse to patch any file
    or section not in WHITELIST_TARGETS below. KOL / Watchlist / 群组 /
    信号金字塔层级排序 are user-proprietary data; Claude must not auto-
    propose changes to them. See memory.md "行为约束" for context — the
    SKM/ShanghaoJin pollution incidents are why this lock exists.
  - **Append-only**. Proposals are immutable once written. /apply moves
    the file to applied/, /reject moves to rejected/. The directory is
    the audit log.
  - **No git**. prompt.md / memory.md are plain files; rollback uses
    .bak.<unix_ts> copies. Each apply makes a backup first.

JSON schema for slash command output (Phase 2b):

    <proposals>
    {
      "command": "reflect|critique|score|kol_drift|heat",
      "generated_at": "2026-05-18T20:55:00-04:00",
      "lookback_days": 7,
      "stats": { ... } | null,        // /score uses this for hit rates
      "proposals": [
        {
          "kind": "positive_experience|negative_experience|gap|rule_update|kol_drift|watchlist_candidate",
          "target_file": "memory.md|prompt.md|null",
          "target_section": "运行经验:正向|null",     // exact section heading
          "patch": "- **2026-05-18**: <规则>",         // exact text to append
          "patch_mode": "append|edit|null",            // append=add to end of section
          "applicable": true,                          // false => human-only action
          "confidence": "high|mid|low",
          "observations": [                            // ≥2 for self-evolution rules
            {"file": "2026-05-17-09-brief.md", "line": 47, "quote": "..."},
            ...
          ],
          "source_kind": "text|ocr|cross_validated|n/a",
          "window_aware": false                        // KOL time-gated channels
        }
      ]
    }
    </proposals>

WHITELIST (applier may auto-patch these — kind => target_file:target_section):

    positive_experience  => memory.md : ## 运行经验:正向
    negative_experience  => memory.md : ## 运行经验:负向
    rule_update          => memory.md : ## 信号优先级金字塔  (CASE-BY-CASE — see _is_writable)
                            prompt.md : ## 行为约束 (or numeric limits in Phase A)

BLACKLIST (applier ALWAYS refuses — surface to user, never auto-apply):

    kol_drift            => prompt.md : ### 大 V X 账号 table     (user-only)
    watchlist_candidate  => prompt.md : ### 个股 watchlist table   (user-only)
    gap (group/channel)  => prompt.md : 微信群 / Discord 频道 lists (user-only)
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path.home() / "Scripts" / "market-brief"
SELFEVOLVE_DIR = SCRIPTS_DIR / "selfevolve"
PROPOSALS_DIR = SELFEVOLVE_DIR / "proposals"
APPLIED_DIR = SELFEVOLVE_DIR / "applied"
REJECTED_DIR = SELFEVOLVE_DIR / "rejected"
BACKUP_DIR = SELFEVOLVE_DIR / "backups"

PROMPT_MD = SCRIPTS_DIR / "prompt.md"
MEMORY_MD = SCRIPTS_DIR / "memory.md"


# ─────────────────────────────────────────────────────────────────────────────
#  Scope lock (applier reads this — DO NOT widen casually)
# ─────────────────────────────────────────────────────────────────────────────

# (kind, target_file_name, allowed_section_substrings_in_heading)
# Applier checks (proposal.target_file, proposal.target_section) against this
# table. If no row matches, the proposal is BLACKLISTED → human-only.
WHITELIST_TARGETS: list[tuple[str, str, tuple[str, ...]]] = [
    ("positive_experience", "memory.md", ("运行经验:正向",)),
    ("negative_experience", "memory.md", ("运行经验:负向",)),
    ("rule_update",         "memory.md", ("信号优先级金字塔",)),
    ("rule_update",         "prompt.md", ("行为约束",)),
]


def is_writable(kind: str, target_file: str | None, target_section: str | None) -> bool:
    """Returns True if a proposal is in the WHITELIST and may be auto-patched.

    Anything else (kol_drift, watchlist_candidate, gap, or rule_update
    targeting a non-whitelisted section) is human-only.
    """
    if not target_file or not target_section:
        return False
    fname = Path(target_file).name  # normalize "memory.md" / "./memory.md"
    for w_kind, w_file, sections in WHITELIST_TARGETS:
        if kind != w_kind:
            continue
        if fname != w_file:
            continue
        if any(s in target_section for s in sections):
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Storage layout
# ─────────────────────────────────────────────────────────────────────────────

def ensure_dirs() -> None:
    for d in (PROPOSALS_DIR, APPLIED_DIR, REJECTED_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
#  JSON extraction (called after Claude returns from _ask_claude)
# ─────────────────────────────────────────────────────────────────────────────

# Match `<proposals>{...}</proposals>` even when JSON has nested `{}`.
# Greedy + last block wins (in case Claude includes an example earlier).
_PROPOSALS_RE = re.compile(r"<proposals>\s*(\{.*\})\s*</proposals>", re.DOTALL)


def extract_proposals_block(reply: str) -> tuple[str, dict | None]:
    """Extract <proposals>{json}</proposals> from Claude's reply.

    Returns (markdown_without_block, parsed_json_or_None).
    If parsing fails or no block is present, returns (reply, None).

    Behavior:
      - Strips the entire <proposals>...</proposals> block (incl. tags)
        from the markdown so the WeChat user doesn't see it.
      - Returns None for json if extraction OR parsing fails — caller
        should still send the markdown to WeChat (this is fail-soft).
    """
    matches = list(_PROPOSALS_RE.finditer(reply))
    if not matches:
        return reply, None
    last = matches[-1]
    try:
        data = json.loads(last.group(1))
    except json.JSONDecodeError:
        # Strip the malformed block anyway so user doesn't see raw JSON
        cleaned = reply[: last.start()] + reply[last.end():]
        return cleaned.rstrip(), None
    cleaned = reply[: last.start()] + reply[last.end():]
    return cleaned.rstrip(), data


# ─────────────────────────────────────────────────────────────────────────────
#  Proposal persistence
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _proposal_id(command: str, idx: int, ts: str) -> str:
    # ts like "2026-05-18T20:55:00-04:00" → compact "20260518T205500"
    try:
        dt = datetime.fromisoformat(ts)
        compact = dt.strftime("%Y%m%dT%H%M%S")
    except (ValueError, TypeError):
        compact = "00000000T000000"
    short_uuid = uuid.uuid4().hex[:4]
    return f"{compact}_{command}_{idx:02d}_{short_uuid}"


def save_proposals(command: str, data: dict) -> list[Path]:
    """Persist each proposal in data['proposals'] as a separate JSON file
    in selfevolve/proposals/. Returns the list of written file paths.

    Side effects:
      - Creates selfevolve/* dirs if missing
      - Updates ~/Scripts/market-brief/last_<command>.json (full payload)
    """
    ensure_dirs()
    ts = data.get("generated_at") or _now_iso()
    data["generated_at"] = ts  # canonicalize if missing
    proposals = data.get("proposals") or []
    written: list[Path] = []
    for i, prop in enumerate(proposals, 1):
        # Stamp each proposal with id + writability flag for the applier
        prop_id = prop.get("id") or _proposal_id(command, i, ts)
        prop["id"] = prop_id
        prop["command"] = command
        prop["generated_at"] = ts
        prop["writable"] = is_writable(
            prop.get("kind", ""),
            prop.get("target_file"),
            prop.get("target_section"),
        )
        out_path = PROPOSALS_DIR / f"{prop_id}.json"
        out_path.write_text(
            json.dumps(prop, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written.append(out_path)
    # Also save full payload as last_<cmd>.json for quick inspection
    summary_path = SCRIPTS_DIR / f"last_{command}.json"
    summary_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return written


def load_proposal(prop_id: str) -> dict | None:
    """Find a proposal by id. Searches proposals/ first, then applied/ and
    rejected/ (so /show <id> works even after /apply or /reject)."""
    for d in (PROPOSALS_DIR, APPLIED_DIR, REJECTED_DIR):
        p = d / f"{prop_id}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
    return None


def list_pending_proposals(max_age_days: int = 30) -> list[dict]:
    """Return all unapplied + unrejected proposals younger than max_age_days,
    sorted by generated_at descending."""
    ensure_dirs()
    cutoff = time.time() - max_age_days * 86400
    out: list[dict] = []
    for p in PROPOSALS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if p.stat().st_mtime < cutoff:
            continue
        out.append(data)
    out.sort(key=lambda d: d.get("generated_at", ""), reverse=True)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  JSON schema prompt epilogue — appended to every slash command's prompt
# ─────────────────────────────────────────────────────────────────────────────

def json_epilogue(command: str, *, stats_schema: str | None = None) -> str:
    """Returns the prompt fragment that instructs Claude to emit
    <proposals>JSON</proposals> at the end of its reply, after the
    human-readable markdown.

    Each command has its own valid `kind` values for proposals:

      reflect    -> positive_experience | negative_experience | rule_update
      critique   -> gap | rule_update
      score      -> rule_update (rare; proposals can be empty array)
      kol_drift  -> kol_drift (always applicable=false — surfaces only)
      heat       -> watchlist_candidate (always applicable=false)

    `stats_schema` is for /score, which emits a `stats` object instead of
    just proposals. Pass the JSON shape verbatim.
    """
    valid_kinds = {
        "reflect":   "positive_experience | negative_experience | rule_update",
        "critique":  "gap | rule_update",
        "score":     "rule_update",
        "kol_drift": "kol_drift",
        "heat":      "watchlist_candidate",
    }
    kinds = valid_kinds.get(command, "rule_update")

    examples = {
        "reflect": (
            '{\n'
            '  "kind": "positive_experience",\n'
            '  "target_file": "memory.md",\n'
            '  "target_section": "## 运行经验:正向",\n'
            '  "patch": "- **2026-05-18**: <具体规则>",\n'
            '  "patch_mode": "append",\n'
            '  "applicable": true,\n'
            '  "confidence": "high",\n'
            '  "observations": [{"file": "2026-05-17-09-brief.md", "line": 47, "quote": "..."}, {"file": "logs/2026-05-17.log", "line": 213, "quote": "..."}],\n'
            '  "source_kind": "text",\n'
            '  "window_aware": false\n'
            '}'
        ),
        "critique": (
            '{\n'
            '  "kind": "gap",\n'
            '  "target_file": null,           // 大 V / Watchlist / 群组列表是用户专属,绝不 propose patch\n'
            '  "target_section": null,\n'
            '  "patch": null,                  // gap 只描述漏了什么,由用户判断怎么改\n'
            '  "patch_mode": null,\n'
            '  "applicable": false,\n'
            '  "confidence": "high",\n'
            '  "observations": [{"file": "稳准狠 chatroom", "line": null, "quote": "牛哥 2026-05-17 14:23: ASTS short thesis..."}],\n'
            '  "source_kind": "text",\n'
            '  "window_aware": false,\n'
            '  "gap_type": "个股漏 | 仓位漏 | 大V漏 | 技术位漏 | 宏观漏 | 速读失真",\n'
            '  "summary": "<≤80 字描述漏了什么>"\n'
            '}'
        ),
        "score": (
            '// score 通常 proposals=[],主要数据在 stats。仅当某 ticker/KOL 命中率\n'
            '// 极低 (≤25%, N≥5) 时才 emit 一个 rule_update proposal:\n'
            '{\n'
            '  "kind": "rule_update",\n'
            '  "target_file": "memory.md",\n'
            '  "target_section": "## 信号优先级金字塔",\n'
            '  "patch": "- **<日期>**:<对哪个 KOL/层级权重的调整建议>",\n'
            '  "patch_mode": "append",\n'
            '  "applicable": true,\n'
            '  "confidence": "mid",\n'
            '  "observations": [{"file": "score_stats", "line": null, "quote": "INTC 7d hit rate 2/10 = 20%"}],\n'
            '  "source_kind": "cross_validated",\n'
            '  "window_aware": false\n'
            '}'
        ),
        "kol_drift": (
            '{\n'
            '  "kind": "kol_drift",\n'
            '  "target_file": null,            // KOL 表 BLACKLISTED — 不 auto-patch\n'
            '  "target_section": null,\n'
            '  "patch": null,\n'
            '  "patch_mode": null,\n'
            '  "applicable": false,\n'
            '  "confidence": "high",\n'
            '  "observations": [{"file": "@handle/tweet_id", "line": null, "quote": "..."}],\n'
            '  "source_kind": "text",\n'
            '  "window_aware": false,\n'
            '  "handle": "imnotharsh",\n'
            '  "current_desc": "<copy from prompt.md>",\n'
            '  "actual_focus": "<observed>",\n'
            '  "suggested_desc": "<新一句话主战场>"\n'
            '}'
        ),
        "heat": (
            '{\n'
            '  "kind": "watchlist_candidate",\n'
            '  "target_file": null,            // Watchlist 表 BLACKLISTED — 不 auto-patch\n'
            '  "target_section": null,\n'
            '  "patch": null,\n'
            '  "patch_mode": null,\n'
            '  "applicable": false,\n'
            '  "confidence": "mid",\n'
            '  "observations": [{"file": "2026-05-17-09-brief.md", "line": 12, "quote": "..."}, {"file": "2026-05-17-12-brief.md", "line": 30, "quote": "..."}],\n'
            '  "source_kind": "text",\n'
            '  "window_aware": false,\n'
            '  "ticker": "XYZ",\n'
            '  "heat_count": 4,\n'
            '  "summary": "<≤80 字为什么值得加 watchlist>"\n'
            '}'
        ),
    }
    example = examples.get(command, examples["reflect"])

    stats_block = ""
    if stats_schema:
        stats_block = (
            "\n  \"stats\": " + stats_schema + ","
        )

    return (
        "\n\n---\n\n"
        "## 必须在 markdown 后面追加一个 `<proposals>...</proposals>` JSON 块\n\n"
        "**目的**:这个 JSON 是给闭环 applier (Phase 2c+2d) 消化的。markdown 给"
        "用户在微信看,JSON 给自动化系统读。**两个都必须输出**。\n\n"
        "**格式**(严格,不要加任何 comment 行进 JSON 体本身):\n\n"
        "```\n"
        "<proposals>\n"
        "{\n"
        f'  "command": "{command}",\n'
        '  "generated_at": "<ISO-8601 with tz, e.g. 2026-05-18T20:55:00-04:00>",\n'
        '  "lookback_days": <int 或 null>,'
        f'{stats_block}\n'
        '  "proposals": [ ... ]              // 见下面 schema\n'
        "}\n"
        "</proposals>\n"
        "```\n\n"
        f"**proposals[].kind 只允许**:`{kinds}`\n\n"
        "**每个 proposal 字段**:\n"
        "- `kind`: 上面允许的值\n"
        "- `target_file`: `\"memory.md\"` | `\"prompt.md\"` | `null` (null = 人审,不 auto-patch)\n"
        "- `target_section`: 完整的 H2 heading,如 `\"## 运行经验:正向\"`,或 null\n"
        "- `patch`: 要 append 的字面 markdown 文本(不含 section heading),或 null\n"
        "- `patch_mode`: `\"append\"` | `\"edit\"` | null\n"
        "- `applicable`: bool — true=可 auto-patch (但仍走人审 /apply);false=只 surface\n"
        "- `confidence`: `\"high\"` | `\"mid\"` | `\"low\"`\n"
        "- `observations`: ≥2 个 `{file, line?, quote}` 对象 (≥1 for /critique gap)。"
        "**单次观察的提议会被 applier 直接拒收** —— 这是反污染的最关键 gate\n"
        "- `source_kind`: `\"text\"` | `\"ocr\"` | `\"cross_validated\"` | `\"n/a\"`\n"
        "- `window_aware`: true = 涉及时段性数据源(月哥观月亭等),applier 会以"
        "更宽松的 absent-tolerance 评估\n\n"
        f"**示例**(`{command}` 适用):\n\n"
        f"```\n{example}\n```\n\n"
        "**没有任何 proposal 时**:输出 `\"proposals\": []`(JSON 块仍要发,只是空数组)。\n\n"
        "**绝对不要**:\n"
        "- 把 JSON 嵌在 markdown 中间(必须在末尾)\n"
        "- 在 JSON 里加 `//` 注释或 trailing comma(违反 strict JSON,parser 会拒)\n"
        "- 给 `kol_drift` / `watchlist_candidate` / `gap` 配 `target_file`(它们是 BLACKLISTED,"
        "applier 看到 target_file=non-null 会直接拒,反而损失这条 proposal 的可见性)\n"
    )
