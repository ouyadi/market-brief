"""applier.py — Phase 2c+2d closed-loop patch applier with human review.

Reads proposals from selfevolve/proposals/, lets the user approve via
WeChat slash commands (/apply <id> or /reject <id>), patches the target
file with a timestamped backup, and supports rollback.

Public API used by listen_weixin.py:

    list_pending(max_age_days=30) -> str    (formatted markdown)
    show_proposal(prop_id)         -> str   (markdown with diff preview)
    apply_proposal(prop_id)        -> str   (markdown status; performs patch)
    reject_proposal(prop_id)       -> str   (markdown status; moves to rejected/)
    rollback_file(file_name)       -> str   (markdown status; restores .bak)

Safety invariants (DO NOT relax without re-reading memory.md 行为约束):

  1. Scope-lock: a proposal's `writable` flag must be True (set by selfevolve
     based on WHITELIST_TARGETS). Even if Claude emits target_file pointing
     at a blacklisted section, save_proposals stamped writable=False, and
     this applier refuses with a clear error.
  2. Backup-first: every apply_proposal creates a .bak.<unix_ts> next to the
     target file BEFORE touching it. rollback_file restores the newest .bak.
  3. Append-only mode for self-evolution: patch_mode="append" inserts the
     patch text at the END of the target_section. patch_mode="edit" is more
     dangerous and currently NOT implemented — Claude must propose `append`
     for the whitelisted sections (运行经验:正向/负向, 行为约束 are all
     append-friendly).
  4. Idempotency: if the same proposal id is /apply'd twice, the second call
     no-ops with a clear "already applied" message.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

import selfevolve as se


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

def list_pending(max_age_days: int = 30) -> str:
    """Return formatted markdown list of pending proposals, newest first.
    Used by /proposals slash command."""
    proposals = se.list_pending_proposals(max_age_days=max_age_days)
    if not proposals:
        return f"## 待审 proposals\n\n暂无 (近 {max_age_days} 天)。"

    lines = [f"## 待审 proposals ({len(proposals)} 个,近 {max_age_days} 天)"]
    lines.append("")
    for i, p in enumerate(proposals, 1):
        kind = p.get("kind", "?")
        target = p.get("target_file") or "—"
        section = p.get("target_section") or ""
        if section:
            target = f"{target} :: {section[:30]}{'…' if len(section)>30 else ''}"
        conf = p.get("confidence", "?")
        writable = "✓" if p.get("writable") else "🔒"
        prop_id = p.get("id", "?")
        # 1-line summary: first 60 chars of patch or summary
        summary = p.get("summary") or p.get("patch") or p.get("suggested_desc") or ""
        summary = summary.replace("\n", " ").strip()[:60]
        lines.append(f"{i}. `{prop_id}` {writable} **{kind}** [conf={conf}]")
        lines.append(f"   → {target}")
        if summary:
            lines.append(f"   {summary}{'…' if len(summary)==60 else ''}")
        lines.append("")
    lines.append("---")
    lines.append("`/show <id>` 看完整 diff,`/apply <id>` 落盘,`/reject <id>` 丢弃")
    lines.append("(✓ = 可 auto-patch,🔒 = 用户专有数据/盲点,只是 surface)")
    return "\n".join(lines)


def show_proposal(prop_id: str) -> str:
    """Show full proposal + diff preview. Used by /show <id> slash command."""
    p = se.load_proposal(prop_id)
    if p is None:
        return f"✗ 没找到 proposal `{prop_id}`。`/proposals` 看完整列表。"

    state = _proposal_state(prop_id)
    state_emoji = {"pending": "⏳", "applied": "✅", "rejected": "❌"}.get(state, "?")

    lines = [
        f"## Proposal `{prop_id}` ({state_emoji} {state})",
        "",
        f"- **kind**: `{p.get('kind')}`",
        f"- **command**: `/{p.get('command', '?')}`",
        f"- **生成于**: {p.get('generated_at', '?')}",
        f"- **置信度**: `{p.get('confidence', '?')}`",
        f"- **source_kind**: `{p.get('source_kind', '?')}`",
        f"- **window_aware**: `{p.get('window_aware', False)}`",
        f"- **可 auto-patch**: {'✓' if p.get('writable') else '🔒 (BLACKLISTED — 只能人手改)'}",
        "",
    ]

    if p.get("target_file"):
        lines.append(f"**target_file**: `{p['target_file']}`")
    if p.get("target_section"):
        lines.append(f"**target_section**: `{p['target_section']}`")
    if p.get("patch_mode"):
        lines.append(f"**patch_mode**: `{p['patch_mode']}`")
    lines.append("")

    # Show observations (evidence)
    obs = p.get("observations") or []
    if obs:
        lines.append(f"**观察依据 ({len(obs)} 条)**:")
        for o in obs[:5]:
            file = o.get("file", "?")
            line_no = o.get("line")
            quote = (o.get("quote") or "").replace("\n", " ").strip()[:80]
            loc = f"{file}:{line_no}" if line_no else file
            lines.append(f"- `{loc}` — {quote}{'…' if len(quote)==80 else ''}")
        if len(obs) > 5:
            lines.append(f"- … 还有 {len(obs)-5} 条")
        lines.append("")

    # Kind-specific fields
    if p.get("summary"):
        lines.append(f"**summary**: {p['summary']}")
        lines.append("")
    if p.get("gap_type"):
        lines.append(f"**gap_type**: `{p['gap_type']}`")
        lines.append("")
    if p.get("handle"):
        lines.append(f"**KOL handle**: `@{p['handle']}`")
        lines.append(f"**当前描述**: {p.get('current_desc', '?')}")
        lines.append(f"**实际焦点**: {p.get('actual_focus', '?')}")
        lines.append(f"**建议新描述**: {p.get('suggested_desc', '?')}")
        lines.append("")
    if p.get("ticker"):
        lines.append(f"**ticker**: `${p['ticker']}` (热度 {p.get('heat_count', '?')})")
        lines.append("")

    # Diff preview if applicable
    if p.get("patch") and p.get("target_file") and p.get("target_section"):
        diff = _generate_diff_preview(p)
        if diff:
            lines.append("**Diff preview**:")
            lines.append("```diff")
            lines.append(diff)
            lines.append("```")
            lines.append("")

    if state == "pending":
        if p.get("writable"):
            lines.append(f"`/apply {prop_id}` 落盘,`/reject {prop_id}` 丢弃")
        else:
            lines.append(f"⚠️ 这条 proposal 标记为 BLACKLISTED — applier 不会 auto-patch,只是 surface。")
            lines.append(f"如要采纳,人手改 `{p.get('target_file', '?')}`,然后 `/reject {prop_id}` 关闭这条记录。")
    return "\n".join(lines)


def apply_proposal(prop_id: str) -> str:
    """Apply a proposal: backup target, append patch text to target_section,
    move proposal to applied/. Used by /apply <id> slash command."""
    se.ensure_dirs()

    p = se.load_proposal(prop_id)
    if p is None:
        return f"✗ 没找到 proposal `{prop_id}`。`/proposals` 看完整列表。"

    state = _proposal_state(prop_id)
    if state == "applied":
        return f"⚠️ `{prop_id}` 已经 apply 过了 (再 apply 会重复 patch)。`/show {prop_id}` 看详情。"
    if state == "rejected":
        return f"⚠️ `{prop_id}` 之前被 reject 过。先 `/show {prop_id}` 确认要重新启用,然后人手 mv 回 proposals/。"

    if not p.get("writable"):
        return (
            f"🔒 `{prop_id}` 是 BLACKLISTED 提议 (kind=`{p.get('kind')}`),不能 auto-patch。\n"
            f"原因:`{p.get('target_file') or '<no target>'}`/`{p.get('target_section') or '<no section>'}` 不在白名单。\n"
            f"白名单仅含 memory.md 的 ## 运行经验:正向/负向 / ## 信号优先级金字塔,"
            f"以及 prompt.md 的 ## 行为约束。\n"
            f"`/reject {prop_id}` 关闭这条记录,或者人手改文件。"
        )

    target_file = p.get("target_file")
    target_section = p.get("target_section")
    patch = p.get("patch")
    patch_mode = p.get("patch_mode") or "append"

    if not (target_file and target_section and patch):
        return f"✗ `{prop_id}` 缺少 target_file / target_section / patch 字段,无法 apply。"
    if patch_mode != "append":
        return f"✗ patch_mode=`{patch_mode}` 当前未实现 (仅支持 append)。`/reject` 后人手改。"

    # Resolve target_file to absolute path
    path = se.SCRIPTS_DIR / Path(target_file).name
    if not path.exists():
        return f"✗ target file `{path}` 不存在。"

    # Backup first
    backup_path = _make_backup(path)
    log_msg = f"backup → {backup_path.name}"

    # Read, append patch to the right section, write back
    try:
        original = path.read_text(encoding="utf-8")
        patched = _append_to_section(original, target_section, patch)
        if patched is None:
            return f"✗ 找不到 section `{target_section}` in `{path.name}`。文件可能被改过,先 /show 重新确认。"
        path.write_text(patched, encoding="utf-8")
    except Exception as exc:
        return f"✗ patch 失败: {exc}\n备份在 `{backup_path.name}`,文件未被改。"

    # Move proposal file to applied/
    src = se.PROPOSALS_DIR / f"{prop_id}.json"
    dst = se.APPLIED_DIR / f"{prop_id}.json"
    if src.exists():
        src.rename(dst)

    return (
        f"✅ 已 apply `{prop_id}`\n\n"
        f"- patched: `{path.name}` :: {target_section}\n"
        f"- {log_msg}\n"
        f"- 出问题:`/rollback {Path(target_file).name}` 回退最近一次"
    )


def reject_proposal(prop_id: str) -> str:
    """Move proposal to rejected/. Used by /reject <id> slash command."""
    se.ensure_dirs()

    p = se.load_proposal(prop_id)
    if p is None:
        return f"✗ 没找到 proposal `{prop_id}`。"

    state = _proposal_state(prop_id)
    if state == "rejected":
        return f"⚠️ `{prop_id}` 已经 reject 过。"
    if state == "applied":
        return (
            f"⚠️ `{prop_id}` 已经 apply 过 (落盘了)。"
            f"想撤销改动用 `/rollback {Path(p.get('target_file','')).name}`。"
        )

    src = se.PROPOSALS_DIR / f"{prop_id}.json"
    dst = se.REJECTED_DIR / f"{prop_id}.json"
    if src.exists():
        src.rename(dst)
        return f"❌ 已 reject `{prop_id}`。如果以后想恢复,人手 mv 回 proposals/。"
    return f"✗ 异常状态:proposal file 不在 proposals/ 也不在 applied/rejected/。"


def rollback_file(file_name: str) -> str:
    """Restore the latest .bak.<ts> of `file_name` (prompt.md or memory.md).
    Used by /rollback <file> slash command."""
    se.ensure_dirs()

    fname = Path(file_name).name
    if fname not in ("prompt.md", "memory.md"):
        return f"✗ 只能 rollback `prompt.md` 或 `memory.md`,不能 `{fname}`。"

    target = se.SCRIPTS_DIR / fname
    backups = sorted(
        se.BACKUP_DIR.glob(f"{fname}.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not backups:
        return f"✗ 没有 `{fname}` 的备份记录。从未 apply 过 patch。"

    latest = backups[0]
    # Re-backup current state before rollback (so /rollback is itself reversible)
    pre_rollback = _make_backup(target, suffix=".pre-rollback")

    try:
        shutil.copy2(latest, target)
    except Exception as exc:
        return f"✗ rollback 失败: {exc}\n当前文件备份在 `{pre_rollback.name}`。"

    bak_age_s = int(time.time() - latest.stat().st_mtime)
    bak_age = _human_duration(bak_age_s)
    return (
        f"⏪ 已 rollback `{fname}` 到 `{latest.name}` ({bak_age} 前的版本)\n\n"
        f"- 之前的状态(rollback 之前的)备份在 `{pre_rollback.name}`,\n"
        f"  想撤销 rollback 就用这个 .pre-rollback 替换文件。"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Internals
# ─────────────────────────────────────────────────────────────────────────────

def _proposal_state(prop_id: str) -> str:
    """Return 'pending', 'applied', 'rejected', or 'unknown'."""
    if (se.PROPOSALS_DIR / f"{prop_id}.json").exists():
        return "pending"
    if (se.APPLIED_DIR / f"{prop_id}.json").exists():
        return "applied"
    if (se.REJECTED_DIR / f"{prop_id}.json").exists():
        return "rejected"
    return "unknown"


def _make_backup(path: Path, suffix: str = "") -> Path:
    """Copy path to selfevolve/backups/<name>.bak.<unix_ts><suffix>."""
    ts = int(time.time())
    base_name = f"{path.name}.bak.{ts}"
    bak_path = se.BACKUP_DIR / f"{base_name}{suffix}"
    i = 1
    while bak_path.exists():
        bak_path = se.BACKUP_DIR / f"{base_name}.{i}{suffix}"
        i += 1
    shutil.copy2(path, bak_path)
    return bak_path


def _append_to_section(content: str, section_heading: str, patch: str) -> str | None:
    """Append `patch` (with surrounding newlines as needed) to the end of
    the section starting with `section_heading` (e.g. "## 运行经验:正向").

    Returns the new file content, or None if section_heading not found.

    Section ends at the next H2 (`## `) heading or EOF.
    """
    lines = content.splitlines(keepends=False)
    target_idx = None

    wanted = section_heading.strip()
    for i, line in enumerate(lines):
        if line.strip() == wanted:
            target_idx = i
            break

    # Proposals intentionally use stable short section names such as
    # "## 运行经验:正向"; the live heading may carry a parenthetical suffix.
    if target_idx is None:
        wanted_key = wanted[3:].strip() if wanted.startswith("## ") else wanted
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("## ") and wanted_key and wanted_key in stripped:
                target_idx = i
                break

    if target_idx is None:
        return None

    # Find the end of this section: next H2 line (`## ...`) or EOF
    end_idx = len(lines)
    for j in range(target_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end_idx = j
            break

    # Trim trailing blank lines within the section, then re-add a single blank line + patch
    section_end = end_idx
    while section_end > target_idx + 1 and lines[section_end - 1].strip() == "":
        section_end -= 1

    # Build new content
    new_lines = lines[:section_end]
    new_lines.append("")  # blank line before patch
    # patch may itself be multiple lines
    new_lines.extend(patch.rstrip().splitlines())
    new_lines.append("")  # blank line after patch
    new_lines.extend(lines[end_idx:])

    # Preserve trailing newline if original had one
    suffix = "\n" if content.endswith("\n") else ""
    return "\n".join(new_lines).rstrip("\n") + suffix


def _generate_diff_preview(p: dict) -> str | None:
    """Build a small +/- diff snippet showing what the apply would add."""
    target_section = p.get("target_section") or ""
    patch = p.get("patch") or ""
    if not patch:
        return None
    lines = [f"  {target_section}", "  ..."]
    for pl in patch.rstrip().splitlines():
        lines.append(f"+ {pl}")
    return "\n".join(lines)


def _human_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}天{(seconds % 86400) // 3600}h"
