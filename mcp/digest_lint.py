"""Deterministic lint for the generated 微信速读 section.

Called by run.ps1 after the report is written and before the WeChat push.
WARN-ONLY by design: exit 1 just makes the launcher log the problems; the
push always continues. Counts the section BODY (heading excluded) because
push_weixin.py pushes with bare=True.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from extract_section import extract_section

MAX_CHARS = 1900
# (delimiter, exact_match). Emoji delimiters match by prefix, text ones exactly,
# so a body line merely mentioning "AlphaLens" cannot false-positive.
SECTION_ORDER = [
    ("⚡", False),
    ("🎙", False),
    ("🏛", False),
    ("AlphaLens", True),
    ("复盘", True),
    ("📊", False),
]
INCREMENT_RE = re.compile(r"^本轮增量[:：]")
SHORT_RE = re.compile(r"^本轮增量[:：]\s*无\s*$")
SETUP_HEAD_RE = re.compile(r"^(\d+)\.\s*(?:\[(新|更新|延续)\])?\s*(.*)$")
TRIGGER_RE = re.compile(r"触发[:：]")


def _delimiter_hits(lines: list[str]) -> list[tuple[int, int]]:
    hits = []
    for i, raw in enumerate(lines):
        s = raw.strip()
        for order, (delim, exact) in enumerate(SECTION_ORDER):
            if (s == delim) if exact else s.startswith(delim):
                hits.append((i, order))
                break
    return hits


def lint_digest(body: str) -> list[str]:
    problems: list[str] = []
    if len(body) > MAX_CHARS:
        problems.append(f"LINT: 字数超限 {len(body)} > {MAX_CHARS}")

    lines = body.splitlines()
    inc_lines = [ln.strip() for ln in lines if INCREMENT_RE.match(ln.strip())]
    if not inc_lines:
        problems.append("LINT: 缺少「本轮增量:」行")
        is_short = False
    else:
        is_short = bool(SHORT_RE.match(inc_lines[0]))
    if is_short:
        return problems  # 极短版:只约束字数与增量行

    hits = _delimiter_hits(lines)
    orders = [o for _, o in hits]
    if orders != sorted(orders):
        problems.append("LINT: section 顺序不符骨架(⚡→🎙→🏛→AlphaLens→复盘→📊)")

    # setups live between the ⚡ delimiter and the next delimiter
    flash_rows = [i for i, o in hits if o == 0]
    if flash_rows:
        start = flash_rows[0] + 1
        later = [i for i, _ in hits if i > flash_rows[0]]
        end = later[0] if later else len(lines)
        setup_idxs = [
            i for i in range(start, end) if SETUP_HEAD_RE.match(lines[i].strip()) and lines[i].strip()[0].isdigit()
        ]
        for k, i in enumerate(setup_idxs):
            m = SETUP_HEAD_RE.match(lines[i].strip())
            num, tag = m.group(1), m.group(2)
            if not tag:
                problems.append(f"LINT: setup #{num} 首行缺 [新/更新/延续] 标记")
                continue
            block_end = setup_idxs[k + 1] if k + 1 < len(setup_idxs) else end
            block = "\n".join(lines[i:block_end])
            if tag in ("新", "更新") and not TRIGGER_RE.search(block):
                problems.append(f"LINT: setup #{num} [{tag}] 缺「触发:」行")
    return problems


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Lint the report's 微信速读 section.")
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    try:
        markdown = args.report.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError: a non-UTF-8 report must warn,
        # not traceback (warn-only contract).
        print("LINT: 报告文件不可读")
        return 1
    section = extract_section(markdown, "微信速读")
    if section is None:
        print("LINT: 报告缺少「微信速读」section")
        return 1
    body = section.split("\n", 1)[1].strip()  # drop the H2 heading line
    problems = lint_digest(body)
    if problems:
        for p in problems:
            print(p)
        return 1
    print("DIGEST_LINT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
