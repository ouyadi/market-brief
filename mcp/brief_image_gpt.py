"""Generate a GPT-image market brief poster from a markdown report.

This is the high-aesthetic path: the model designs an infographic-style image
from the already-written text brief. It requires an OpenAI-compatible Images
API key in MARKET_BRIEF_OPENAI_API_KEY or OPENAI_API_KEY. If no key is present
the script exits quickly so run.ps1 can fall back to the 1990-char text brief.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


DEFAULT_MODEL = "gpt-image-1.5"
DEFAULT_SIZE = "1024x1536"


def _read_key() -> str:
    return (
        os.environ.get("MARKET_BRIEF_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    ).strip()


def _base_url() -> str:
    base = (
        os.environ.get("MARKET_BRIEF_OPENAI_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).strip()
    return base.rstrip("/")


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--[\s\S]*?-->", "", text)


def _compact_brief(markdown: str, max_chars: int = 14500) -> str:
    text = _strip_comments(markdown)
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        # Keep the phone summary, core sections, and concrete bullets. Drop very
        # deep prose once the prompt is already rich enough.
        lines.append(line)
        if sum(len(x) + 1 for x in lines) >= max_chars:
            break
    return "\n".join(lines)


def build_prompt(markdown: str) -> str:
    brief = _compact_brief(markdown)
    return f"""Create a polished vertical Chinese market-brief infographic poster.

Use the supplied markdown brief as the ONLY source of facts. Preserve every
ticker, price, percentage, time, and direction exactly when you choose to show
it. Do not invent logos, prices, tickers, sources, or market data.

Visual target:
- Dark navy financial terminal style, like a premium trading dashboard.
- Dense but readable on a phone, 1024x1536 portrait.
- Big title row, market-hours badge, source/check row.
- Top watchlist block with 3-5 tickers, price/percent, trigger and invalidation.
- Side/right or lower panels for market snapshot, key voices, themes, and
  cross-V signal summary when present.
- Use compact cards, thin grid lines, green/red market colors, and small sparklines
  or chart-like decorative elements only as visual summaries.
- Chinese typography, no lorem ipsum, no fake disclaimer beyond a tiny footer.

If the source text is too long, prioritize: 1) high-priority watchlist, 2) ticker
themes, 3) key voices, 4) macro/policy, 5) cross-V signal summary.

SOURCE MARKDOWN:
{brief}
"""


def generate_image(prompt: str, output: Path, model: str, size: str, quality: str) -> None:
    key = _read_key()
    if not key:
        raise RuntimeError("missing MARKET_BRIEF_OPENAI_API_KEY or OPENAI_API_KEY")

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": 1,
        "output_format": "png",
        "background": "opaque",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{_base_url()}/images/generations",
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Images API HTTP {exc.code}: {body[:800]}") from exc

    image_data = (response.get("data") or [{}])[0].get("b64_json")
    if not image_data:
        raise RuntimeError(f"Images API response missing b64_json: {str(response)[:800]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(image_data))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a GPT-image PNG from a market brief markdown file.")
    parser.add_argument("path", type=Path, help="Markdown report path.")
    parser.add_argument("--output", "-o", type=Path, help="PNG output path.")
    parser.add_argument("--model", default=os.environ.get("MARKET_BRIEF_GPT_IMAGE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--size", default=os.environ.get("MARKET_BRIEF_GPT_IMAGE_SIZE", DEFAULT_SIZE))
    parser.add_argument("--quality", default=os.environ.get("MARKET_BRIEF_GPT_IMAGE_QUALITY", "medium"))
    args = parser.parse_args()

    if not args.path.exists():
        print(f"[ERROR] file not found: {args.path}", file=sys.stderr)
        return 2
    output = args.output or args.path.with_suffix(".gpt.png")
    try:
        prompt = build_prompt(args.path.read_text(encoding="utf-8"))
        generate_image(prompt, output, model=args.model, size=args.size, quality=args.quality)
    except Exception as exc:
        print(f"[ERROR] GPT image generation failed: {exc}", file=sys.stderr)
        return 3
    print(f"GPT_IMAGE_WRITTEN: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
