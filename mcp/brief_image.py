"""Render a market brief markdown file into a phone-friendly PNG poster.

The LLM remains responsible for the factual brief text. This renderer is
deterministic: it lays out the exact markdown content into a dark, dense,
WeChat-readable image so ticker/price/percent text is not hallucinated by an
image model.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re
from pathlib import Path

from playwright.async_api import async_playwright


SECTION_ORDER = (
    "⚡",
    "🎯",
    "🎙",
    "🏛",
    "🏦",
    "🔥",
    "📊",
    "📱",
)


def split_sections(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    lines = markdown.splitlines()
    h2_re = re.compile(r"^##\s+(.*)$")
    h2_idxs = [i for i, line in enumerate(lines) if h2_re.match(line)]
    if not h2_idxs:
        return markdown.strip(), []
    header = "\n".join(lines[: h2_idxs[0]]).strip()
    sections: list[tuple[str, str]] = []
    for pos, idx in enumerate(h2_idxs):
        title = h2_re.match(lines[idx]).group(1).strip()
        end = h2_idxs[pos + 1] if pos + 1 < len(h2_idxs) else len(lines)
        body = "\n".join(lines[idx + 1 : end]).strip()
        sections.append((title, body))
    return header, sections


def pick_sections(sections: list[tuple[str, str]], full: bool) -> list[tuple[str, str]]:
    if full:
        picked = sections
    else:
        picked = []
        for key in SECTION_ORDER:
            for title, body in sections:
                if key in title and body.strip():
                    picked.append((title, body))
                    break
            if len(picked) >= 5:
                break
    # Drop the text-only phone summary from the image when richer sections exist.
    richer = [(t, b) for t, b in picked if "📱" not in t]
    return richer or picked


def inline_md(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def body_to_html(body: str, max_items: int) -> str:
    parts: list[str] = []
    item_count = 0
    in_quote = False
    for raw in body.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_quote:
                parts.append("</div>")
                in_quote = False
            continue
        if stripped.startswith("<!--"):
            continue
        if stripped.startswith("### "):
            parts.append(f"<h3>{inline_md(stripped[4:].strip())}</h3>")
            continue
        if stripped.startswith(">"):
            if not in_quote:
                parts.append('<div class="quote">')
                in_quote = True
            parts.append(f"<p>{inline_md(stripped.lstrip('>').strip())}</p>")
            continue
        if in_quote:
            parts.append("</div>")
            in_quote = False
        if stripped.startswith("- "):
            item_count += 1
            if item_count > max_items:
                continue
            parts.append(f'<p class="bullet">{inline_md(stripped[2:].strip())}</p>')
        else:
            parts.append(f"<p>{inline_md(stripped)}</p>")
    if in_quote:
        parts.append("</div>")
    return "\n".join(parts)


def header_html(header: str) -> tuple[str, str]:
    lines = [line.strip() for line in header.splitlines() if line.strip()]
    title = lines[0].lstrip("# ").strip() if lines else "Market Brief"
    meta = [line.lstrip("> ").strip() for line in lines[1:4]]
    return inline_md(title), " · ".join(html.escape(m) for m in meta if m)


def build_html(markdown: str, full: bool = False, max_items: int = 5) -> str:
    header, sections = split_sections(markdown)
    title, meta = header_html(header)
    cards = []
    for title_text, body in pick_sections(sections, full=full):
        cards.append(
            f"""
            <section class="card">
              <h2>{inline_md(title_text)}</h2>
              <div class="body">{body_to_html(body, max_items=max_items)}</div>
            </section>
            """
        )
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    width: 1080px;
    background: #06111f;
    color: #edf5ff;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
  }}
  .poster {{
    width: 1080px;
    min-height: 1500px;
    padding: 28px;
    background:
      radial-gradient(circle at 10% 0%, rgba(31, 111, 161, .42), transparent 35%),
      linear-gradient(135deg, #07192d 0%, #03101e 58%, #08182a 100%);
  }}
  .top {{
    border: 1px solid rgba(94, 169, 255, .45);
    border-radius: 10px;
    padding: 22px 24px;
    background: rgba(7, 28, 49, .82);
    box-shadow: 0 16px 50px rgba(0, 0, 0, .28);
  }}
  h1 {{
    margin: 0 0 12px;
    font-size: 36px;
    line-height: 1.15;
    letter-spacing: 0;
  }}
  .meta {{
    color: #c4d7ea;
    font-size: 17px;
    line-height: 1.55;
  }}
  .grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 12px;
  }}
  .card {{
    border: 1px solid rgba(94, 169, 255, .35);
    border-radius: 10px;
    background: rgba(8, 31, 52, .88);
    overflow: hidden;
  }}
  .card:nth-child(1) {{ grid-column: 1 / -1; }}
  h2 {{
    margin: 0;
    padding: 12px 16px;
    font-size: 23px;
    line-height: 1.2;
    color: #ffd54a;
    background: linear-gradient(90deg, rgba(30, 79, 119, .92), rgba(16, 45, 72, .55));
    border-bottom: 1px solid rgba(94, 169, 255, .28);
  }}
  .body {{
    padding: 12px 16px 16px;
    font-size: 18px;
    line-height: 1.48;
  }}
  h3 {{
    margin: 12px 0 8px;
    font-size: 20px;
    color: #61c9ff;
  }}
  p {{ margin: 0 0 9px; }}
  .bullet {{
    padding-left: 16px;
    position: relative;
  }}
  .bullet::before {{
    content: "";
    position: absolute;
    left: 0;
    top: .72em;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #3ee56a;
  }}
  strong {{ color: #ffffff; font-weight: 800; }}
  code {{
    color: #8fe1ff;
    background: rgba(106, 201, 255, .12);
    padding: 1px 5px;
    border-radius: 4px;
  }}
  .quote {{
    border-left: 3px solid #3ee56a;
    padding-left: 12px;
    color: #cfe5f6;
    margin-bottom: 8px;
  }}
  .foot {{
    margin-top: 12px;
    color: #8aa7bd;
    font-size: 13px;
    text-align: right;
  }}
</style>
</head>
<body>
  <main class="poster">
    <header class="top">
      <h1>{title}</h1>
      <div class="meta">{meta}</div>
    </header>
    <div class="grid">{''.join(cards)}</div>
    <div class="foot">Generated from deterministic markdown render. Ask WeChat for ticker/section details.</div>
  </main>
</body>
</html>"""


async def render(markdown_path: Path, output_path: Path, full: bool, max_items: int) -> None:
    markdown = markdown_path.read_text(encoding="utf-8")
    html_text = build_html(markdown, full=full, max_items=max_items)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = output_path.with_suffix(".html")
    html_path.write_text(html_text, encoding="utf-8")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1080, "height": 1600}, device_scale_factor=1)
        await page.goto(html_path.as_uri(), wait_until="networkidle")
        poster = page.locator(".poster")
        await poster.screenshot(path=str(output_path))
        await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a market brief markdown file to PNG.")
    parser.add_argument("path", type=Path, help="Markdown report path.")
    parser.add_argument("--output", "-o", type=Path, help="PNG output path.")
    parser.add_argument("--full", action="store_true", help="Render more sections instead of the compact poster.")
    parser.add_argument("--max-items", type=int, default=5, help="Max bullet items per section.")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"[ERROR] file not found: {args.path}")
        return 2
    output = args.output or args.path.with_suffix(".png")
    asyncio.run(render(args.path, output, full=args.full, max_items=args.max_items))
    print(f"IMAGE_WRITTEN: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
