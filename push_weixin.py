"""
Push a markdown file to WeChat via Hermes Agent's iLink adapter.

Usage:
  python push_weixin.py <markdown_file>
  python push_weixin.py <markdown_file> --section "⚡"
  python push_weixin.py --message "short literal message"

Reads credentials from ~/.hermes/.env (written by qr_login_bootstrap.py).
Falls back to ~/.hermes/weixin/accounts/<id>.json if .env is incomplete.

When --section is given, only the H2 section whose heading contains that
substring is pushed, prefixed with the report's pre-H2 header (title +
mode + scan-window metadata). This keeps each push to ~1 chunk and stays
well under iLink's ~10/session quota.

Exit codes:
  0 success, 2 config error, 3 send error, 4 section not found.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from gateway.platforms.weixin import send_weixin_direct

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def extract_section(markdown: str, needle: str) -> str | None:
    """
    Return the report header (everything before the first H2) + the H2 section
    whose heading contains ``needle``. Returns None if no matching section.
    """
    lines = markdown.splitlines()
    h2_re = re.compile(r"^##\s+(.*)$")

    # Find indices of all H2 lines + the index of the first one (= where header ends).
    h2_idxs = [i for i, ln in enumerate(lines) if h2_re.match(ln)]
    if not h2_idxs:
        return None

    header = "\n".join(lines[: h2_idxs[0]]).rstrip()

    needle_lower = needle.lower()
    for pos, idx in enumerate(h2_idxs):
        title = h2_re.match(lines[idx]).group(1)
        if needle_lower in title.lower():
            end = h2_idxs[pos + 1] if pos + 1 < len(h2_idxs) else len(lines)
            section = "\n".join(lines[idx:end]).rstrip()
            return f"{header}\n\n{section}\n" if header else section + "\n"
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _load_credentials() -> dict[str, str]:
    """Discover WEIXIN_* config the same way Hermes does."""
    env = _parse_env_file(HERMES_HOME / ".env")
    account_id = env.get("WEIXIN_ACCOUNT_ID", "").strip()
    token = env.get("WEIXIN_TOKEN", "").strip()
    base_url = env.get("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com").strip()
    home_channel = env.get("WEIXIN_HOME_CHANNEL", "").strip()
    cdn_base_url = env.get("WEIXIN_CDN_BASE_URL", "").strip()

    if not (account_id and token and home_channel):
        accounts_dir = HERMES_HOME / "weixin" / "accounts"
        if accounts_dir.is_dir():
            for f in sorted(accounts_dir.glob("*.json")):
                name = f.name
                if ".context-tokens." in name or ".sync." in name:
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                account_id = account_id or str(data.get("account_id", "")).strip()
                token = token or str(data.get("token", "")).strip()
                base_url = base_url or str(data.get("base_url", "")).strip()
                if not home_channel:
                    home_channel = str(data.get("user_id", "")).strip()
                if account_id and token and home_channel:
                    break

    return {
        "account_id": account_id,
        "token": token,
        "base_url": base_url,
        "cdn_base_url": cdn_base_url,
        "home_channel": home_channel,
    }


async def _push(message: str) -> int:
    creds = _load_credentials()
    missing = [k for k in ("account_id", "token", "home_channel") if not creds[k]]
    if missing:
        print(
            f"[ERROR] missing WEIXIN_* config: {', '.join(missing)}. "
            f"Run qr_login_bootstrap.py first.",
            file=sys.stderr,
        )
        return 2

    extra = {"account_id": creds["account_id"], "base_url": creds["base_url"]}
    if creds["cdn_base_url"]:
        extra["cdn_base_url"] = creds["cdn_base_url"]

    result = await send_weixin_direct(
        extra=extra,
        token=creds["token"],
        chat_id=creds["home_channel"],
        message=message,
    )
    if result.get("success"):
        print(
            f"OK: pushed to chat_id={creds['home_channel']} "
            f"(message_id={result.get('message_id')}, "
            f"context_token_used={result.get('context_token_used')})"
        )
        return 0

    err = result.get("error", "unknown error")
    print(f"[ERROR] weixin push failed: {err}", file=sys.stderr)
    return 3


def main() -> int:
    parser = argparse.ArgumentParser(description="Push markdown to WeChat via iLink.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("path", nargs="?", help="Path to a markdown file to push.")
    group.add_argument("--message", "-m", help="Literal message string to push.")
    parser.add_argument(
        "--section",
        "-s",
        help=(
            "Only push the H2 section whose heading contains this substring "
            "(plus the report's pre-H2 header). Ignored when --message is used. "
            'Example: --section "⚡" picks "## ⚡ 高优先级关注".'
        ),
    )
    args = parser.parse_args()

    if args.message:
        text = args.message
    else:
        p = Path(args.path)
        if not p.exists():
            print(f"[ERROR] file not found: {p}", file=sys.stderr)
            return 2
        text = p.read_text(encoding="utf-8")

        if args.section:
            extracted = extract_section(text, args.section)
            if extracted is None:
                print(
                    f"[ERROR] no H2 section contains substring {args.section!r}; "
                    f"file: {p}",
                    file=sys.stderr,
                )
                return 4
            text = extracted

    if not text.strip():
        print("[ERROR] empty message", file=sys.stderr)
        return 2

    return asyncio.run(_push(text))


if __name__ == "__main__":
    sys.exit(main())
