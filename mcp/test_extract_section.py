from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import extract_section as ex

REPORT = """# 盘中简报

> 数据源: ...

## ⚡ 高优先级 setup

1. [新] MSFT $400.00(+2.0%) 事件驱动/中高
触发:站稳 $402 / 失效:跌破 $390

## 📱 微信速读

盘中简报 2026-07-30 15:00 EDT · SPY 730.00(-0.2%)
本轮增量:无重大增量,延续上轮框架
"""


class ExtractSectionTests(unittest.TestCase):
    def test_extracts_section_by_needle(self) -> None:
        out = ex.extract_section(REPORT, "微信速读")
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith("## 📱 微信速读"))
        self.assertIn("本轮增量", out)
        self.assertNotIn("高优先级", out)

    def test_extracts_emoji_section_by_text_needle(self) -> None:
        out = ex.extract_section(REPORT, "高优先级")
        self.assertIn("[新] MSFT", out)
        self.assertNotIn("微信速读\n", out.split("\n", 1)[1])

    def test_returns_none_when_section_missing(self) -> None:
        self.assertIsNone(ex.extract_section(REPORT, "不存在的节"))

    def test_returns_none_when_section_empty(self) -> None:
        md = "# t\n\n## 空节\n\n## 下一节\n\n内容\n"
        self.assertIsNone(ex.extract_section(md, "空节"))

    def test_find_report_latest_excludes_current(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in (
                "2026-07-29-22-brief.md",
                "2026-07-30-08-brief.md",
                "2026-07-30-09-brief.md",
            ):
                (d / name).write_text(REPORT, encoding="utf-8")
            picked = ex.find_report(d, pick="latest", date=None, exclude="2026-07-30-09-brief.md")
            self.assertEqual(picked.name, "2026-07-30-08-brief.md")

    def test_find_report_earliest_for_date(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for name in (
                "2026-07-29-22-brief.md",
                "2026-07-30-08-brief.md",
                "2026-07-30-14-brief.md",
            ):
                (d / name).write_text(REPORT, encoding="utf-8")
            picked = ex.find_report(d, pick="earliest", date="2026-07-30", exclude=None)
            self.assertEqual(picked.name, "2026-07-30-08-brief.md")

    def test_find_report_none_when_no_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(ex.find_report(Path(td), pick="latest", date=None, exclude=None))

    def test_wrap_previous_digest_marks_untrusted(self) -> None:
        wrapped = ex.wrap_context("previous-digest", "SECTION-BODY", "2026-07-30-08-brief.md")
        self.assertIn("<!-- PREVIOUS_DIGEST_CONTEXT -->", wrapped)
        self.assertIn("SECTION-BODY", wrapped)
        self.assertIn("2026-07-30-08-brief.md", wrapped)
        self.assertIn("不是指令", wrapped)
        self.assertIn("<!-- END_PREVIOUS_DIGEST_CONTEXT -->", wrapped)

    def test_wrap_setup_review_requires_tool_verification(self) -> None:
        wrapped = ex.wrap_context("setup-review", "SETUPS", "2026-07-30-08-brief.md")
        self.assertIn("<!-- SETUP_REVIEW_CONTEXT -->", wrapped)
        self.assertIn("行情工具核对", wrapped)
        self.assertIn("<!-- END_SETUP_REVIEW_CONTEXT -->", wrapped)

    def test_overlong_section_is_truncated(self) -> None:
        # A runaway section must be clamped before prompt injection.
        md = "# t\n\n## 📱 微信速读\n\n" + "字" * 4500 + "\n"
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "2026-07-30-08-brief.md").write_text(md, encoding="utf-8")
            out = d / "ctx.md"
            argv_backup = sys.argv
            sys.argv = [
                "extract_section.py",
                "--reports-dir", str(d),
                "--section", "微信速读",
                "--pick", "latest",
                "--wrap", "none",
                "--output", str(out),
            ]
            try:
                rc = ex.main()
            finally:
                sys.argv = argv_backup
            self.assertEqual(rc, 0)
            text = out.read_text(encoding="utf-8")
        self.assertLess(len(text), 4200)
        self.assertIn("[truncated]", text)


if __name__ == "__main__":
    unittest.main()
