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


# iLink hard cap is ~2000 chars per outbound message. Hermes will split at
# that limit using its own block-aware packer, but its packer treats each
# markdown block (paragraph) as atomic and greedy-packs them, which lets
# an H3 heading get packed at the end of chunk N while its content body
# moves to chunk N+1 -- the "断裂感" the user reported. We pre-chunk on
# our side so Hermes' splitter never has to run (each piece <= the cap),
# and we control where the breaks land.
#
# 1990 == Hermes' MAX_MESSAGE_LENGTH (2000) minus a 10-char safety margin
# for any iLink-side off-by-one. Hermes only splits when len > max_length
# (strict greater-than), so 1990 reliably stays in the no-split branch.
# Picking exactly 1990 (vs an earlier 1800) frees ~10% chunk capacity
# and merges adjacent sections that fall in the 1801-1990 range.
_CHUNK_TARGET = 1990


def _find_best_split(s: str) -> int:
    """Return the position to split s at, preferring logical markdown
    boundaries near the END (so each chunk fills up) over earlier ones.

    Priority order (highest = best, picks LATEST match of best available):
      1. Before an H1/H2/H3 heading (heading stays attached to its body
         in the NEXT chunk -- the fix for the "孤儿标题" bug)
      2. Between paragraphs (blank line)
      3. End of a sentence at a line break (。.！？!? + \\n)
      4. Any line break
      5. End of a sentence anywhere (。！？)
      6. Hard truncate at len(s) -- last resort
    """
    for pat in (
        r"\n\n(?=#{1,3} )",                # before a heading
        r"\n\n",                           # paragraph
        r"(?<=[。\.！？!?])\n",             # sentence end + newline
        r"\n",                             # any newline
        r"(?<=[。！？])",                   # Chinese sentence end
        r"(?<=\.)(?= )",                   # English sentence end
    ):
        matches = list(re.finditer(pat, s))
        if matches:
            return matches[-1].end()
    return len(s)


def smart_chunks(text: str, max_len: int = _CHUNK_TARGET) -> list[str]:
    """Split text into chunks of <= max_len at the most logical boundary
    available, greedy-filling each chunk. See _find_best_split for the
    boundary preference order.
    """
    if len(text) <= max_len:
        return [text]
    out: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            out.append(remaining)
            break
        head = remaining[:max_len]
        cut = _find_best_split(head)
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [c for c in out if c]


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

    chunks = smart_chunks(message)
    any_failed = False
    for i, chunk in enumerate(chunks, 1):
        # iLink rate-limits rapid-fire sends. Hermes has its own per-chunk
        # delay (send_chunk_delay_seconds=1.0) but that only applies WITHIN
        # a single send_weixin_direct call's internal chunking, not BETWEEN
        # multiple top-level calls. When we push 3 sections (⚡/🎯/🎙) as
        # 3 separate send_weixin_direct invocations, we need to insert our
        # own gap or iLink ret=-2 rate-limits us starting on chunk 2.
        # 4s between sends keeps us under the limit empirically.
        if i > 1:
            await asyncio.sleep(4)
        result = await send_weixin_direct(
            extra=extra,
            token=creds["token"],
            chat_id=creds["home_channel"],
            message=chunk,
        )
        if result.get("success"):
            tag = f" [{i}/{len(chunks)}]" if len(chunks) > 1 else ""
            print(
                f"OK{tag}: pushed to chat_id={creds['home_channel']} "
                f"(message_id={result.get('message_id')}, "
                f"context_token_used={result.get('context_token_used')}, "
                f"chunk_chars={len(chunk)})"
            )
        else:
            any_failed = True
            err = result.get("error", "unknown error")
            print(f"[ERROR] weixin push chunk {i}/{len(chunks)} failed: {err}",
                  file=sys.stderr)
    return 3 if any_failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Push markdown to WeChat via iLink.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("path", nargs="?", help="Path to a markdown file to push.")
    group.add_argument("--message", "-m", help="Literal message string to push.")
    parser.add_argument(
        "--section",
        "-s",
        action="append",
        default=[],
        help=(
            "Only push the H2 section whose heading contains this substring "
            "(plus the report's pre-H2 header). May be repeated to push "
            "multiple sections as separate iLink messages, in the order given. "
            "Missing sections are skipped with a warning (not fatal). "
            'Example: --section "⚡" --section "🎯" --section "🎙️"'
        ),
    )
    args = parser.parse_args()

    if args.message:
        # Single literal push -- still one message.
        return asyncio.run(_push(args.message))

    p = Path(args.path)
    if not p.exists():
        print(f"[ERROR] file not found: {p}", file=sys.stderr)
        return 2
    text = p.read_text(encoding="utf-8")

    # No --section -> push the entire file as one message (will be chunked by
    # send_weixin_direct if it exceeds the iLink per-message limit).
    if not args.section:
        if not text.strip():
            print("[ERROR] empty file", file=sys.stderr)
            return 2
        return asyncio.run(_push(text))

    # One or more --section: push each as a separate message. Missing sections
    # are skipped (warn, don't fail) so that e.g. 🎙️ 大 V being omitted
    # because there were no fresh KOL tweets in the window doesn't kill the
    # ⚡/🎯 pushes that DO have content.
    any_pushed = False
    any_failed = False
    # iLink rate-limits rapid-fire outbound. Each _push() call goes through
    # send_weixin_direct which has its own intra-message chunk delay, but
    # between separate _push() calls (= separate sections) we need our own
    # gap or iLink ret=-2 fires on section 2/3. 5s empirically clears it.
    import time as _time
    INTER_SECTION_DELAY_S = 5.0
    for idx, needle in enumerate(args.section):
        extracted = extract_section(text, needle)
        if extracted is None:
            print(
                f"[WARN] no H2 section contains substring {needle!r} -- skipping",
                file=sys.stderr,
            )
            continue
        if not extracted.strip():
            print(f"[WARN] section {needle!r} is empty -- skipping", file=sys.stderr)
            continue
        if any_pushed:  # not the first section we're actually sending
            print(f"[INFO] sleeping {INTER_SECTION_DELAY_S}s before next section "
                  f"(iLink rate-limit avoidance)", file=sys.stderr)
            _time.sleep(INTER_SECTION_DELAY_S)
        rc = asyncio.run(_push(extracted))
        if rc == 0:
            any_pushed = True
        else:
            any_failed = True

    if not any_pushed:
        # Nothing went out -- treat as section-not-found for back-compat with
        # the old single-section exit-code-4 contract.
        return 4
    # Partial failures still flag as non-zero so run.sh / run.ps1 can decide
    # whether to fire the email fallback.
    return 3 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
