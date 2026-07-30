from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import digest_lint as dl

FW_COLON = "："  # 全角冒号 ：

GOOD = """盘中简报 2026-07-30 15:00 EDT · SPY 730.00(-0.2%) / QQQ 660.00(-0.3%) / VIX 20.0
本轮增量:MSFT 财报后上修;MU 新增期权异动

⚡ 高优先级 setup

1. [新] MSFT $400.00(+2.0%) 事件驱动/中高
财报 beat,Azure +43%,capex 有需求支撑。
触发:站稳 $402 / 失效:跌破 $390

2. [更新] NVDA $190.00(-1.0%) 观望/中
新增 190C 大单,其余框架不变。
触发:站回 $195 / 失效:跌破 $185

3. [延续] TLT $83.00(-0.5%) 触发/失效位不变

🎙 跨大 V 信号
A:偏鹰 | B:半导体反弹

🏛 宏观/政策
Polymarket:9 月加息 25bp 53.5%。

AlphaLens
页面姿态中性偏防御,降 beta。

📊 大盘技术位
QQQ 日低 655;VIX 未破 22。
"""

SHORT = """盘中简报 2026-07-30 12:00 EDT · SPY 730.00(-0.1%) / QQQ 660.00(-0.1%) / VIX 19.8
本轮增量:无
1. [延续] MSFT $400.00(+0.1%) 触发/失效位不变
2. [延续] TLT $83.00(-0.1%) 触发/失效位不变
"""


class DigestLintTests(unittest.TestCase):
    def test_clean_digest_passes(self) -> None:
        self.assertEqual(dl.lint_digest(GOOD), [])

    def test_short_version_is_exempt(self) -> None:
        self.assertEqual(dl.lint_digest(SHORT), [])

    def test_missing_increment_line(self) -> None:
        bad = GOOD.replace("本轮增量:MSFT 财报后上修;MU 新增期权异动\n", "")
        problems = dl.lint_digest(bad)
        self.assertTrue(any("本轮增量" in p for p in problems))

    def test_setup_without_tag(self) -> None:
        bad = GOOD.replace("1. [新] MSFT", "1. MSFT")
        problems = dl.lint_digest(bad)
        self.assertTrue(any("标记" in p for p in problems))

    def test_new_setup_without_trigger_line(self) -> None:
        bad = GOOD.replace("触发:站稳 $402 / 失效:跌破 $390\n", "")
        problems = dl.lint_digest(bad)
        self.assertTrue(any("触发" in p for p in problems))

    def test_continued_setup_needs_no_trigger_line(self) -> None:
        # GOOD already has a [延续] setup without its own 触发 line -> no problem.
        self.assertEqual([p for p in dl.lint_digest(GOOD) if "延续" in p], [])

    def test_over_length(self) -> None:
        bad = GOOD + "填" * 2000
        problems = dl.lint_digest(bad)
        self.assertTrue(any("1900" in p for p in problems))

    def test_fullwidth_colons_accepted(self) -> None:
        # LLM 生成中文时常用全角冒号(U+FF1A);不得误报缺增量/触发行。
        variant = (
            GOOD.replace("本轮增量:", "本轮增量" + FW_COLON)
            .replace("触发:", "触发" + FW_COLON)
            .replace("失效:", "失效" + FW_COLON)
        )
        self.assertEqual(dl.lint_digest(variant), [])

    def test_unreadable_file_exits_gracefully(self) -> None:
        # 非 UTF-8 报告文件必须 warn+exit 1,不得 traceback(never-throw 承诺)。
        script = Path(__file__).resolve().parent / "digest_lint.py"
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "2026-07-30-15-brief.md"
            bad.write_bytes("盘中简报 本轮增量:无".encode("gbk") + b"\xff")
            result = subprocess.run(
                [sys.executable, str(script), str(bad)],
                capture_output=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"LINT:", result.stdout)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_section_order_violation(self) -> None:
        bad = GOOD.replace(
            "🏛 宏观/政策\nPolymarket:9 月加息 25bp 53.5%。\n\nAlphaLens\n页面姿态中性偏防御,降 beta。",
            "AlphaLens\n页面姿态中性偏防御,降 beta。\n\n🏛 宏观/政策\nPolymarket:9 月加息 25bp 53.5%。",
        )
        problems = dl.lint_digest(bad)
        self.assertTrue(any("顺序" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
