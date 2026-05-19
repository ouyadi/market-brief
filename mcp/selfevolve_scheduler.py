"""Phase 2a scheduler for market-brief self-evolution.

The scheduler intentionally does *not* apply proposals. It runs one low-frequency
self-evolve slash command, persists the generated proposal JSON through the same
path as the WeChat listener, and optionally pushes a short summary to WeChat.

Default weekly rotation, meant to be launched daily at 23:10 local time:

  Mon: /score 14d 3d
  Tue: /heat 8
  Wed: /critique
  Thu: /kol_drift
  Fri: /reflect 7d
  Sat/Sun: skip
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(os.environ.get("MARKET_BRIEF_DIR") or (Path.home() / "Scripts" / "market-brief"))
LOG_DIR = Path(os.environ.get("SELFEVOLVE_LOG_DIR") or (SCRIPTS_DIR / "logs"))
STATE_DIR = SCRIPTS_DIR / "selfevolve"
STATE_PATH = STATE_DIR / "scheduler_state.json"
LOCK_PATH = STATE_DIR / "scheduler.lock"
LOCK_STALE_SECONDS = 3 * 60 * 60
DEFAULT_MIN_INTERVAL_HOURS = 20

DEFAULT_PLAN: dict[int, str] = {
    0: "/score 14d 3d",   # Monday
    1: "/heat 8",         # Tuesday
    2: "/critique",       # Wednesday
    3: "/kol_drift",      # Thursday
    4: "/reflect 7d",     # Friday
}

ENV_TO_STRIP = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_REGION",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_DISABLE_CRON",
    "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
    "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDECODE",
)


@dataclass
class RunResult:
    command_text: str
    cleaned_reply: str
    proposal_files: list[Path]
    emitted_json: bool
    started_at: str
    finished_at: str


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "selfevolve_scheduler.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("selfevolve-scheduler")


log = setup_logging()


def prepare_llm_env() -> None:
    for name in ENV_TO_STRIP:
        os.environ.pop(name, None)
    os.environ["PYTHONUTF8"] = "1"
    backend = (os.environ.get("MARKET_BRIEF_LLM_BACKEND") or "codex").strip().lower()
    os.environ["MARKET_BRIEF_LLM_BACKEND"] = backend
    if backend != "claude":
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return
    secrets_path = SCRIPTS_DIR / "secrets.json"
    if not secrets_path.exists():
        raise RuntimeError(f"secrets.json missing at {secrets_path}")
    data = json.loads(secrets_path.read_text(encoding="utf-8"))
    token = str(data.get("claudeCodeOauthToken") or "").strip()
    if not token:
        raise RuntimeError("secrets.json missing claudeCodeOauthToken")
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"last_success": {}, "runs": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = STATE_PATH.with_suffix(f".bad.{int(time.time())}.json")
        STATE_PATH.rename(backup)
        return {"last_success": {}, "runs": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire_lock(force: bool = False) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if force or age > LOCK_STALE_SECONDS:
            LOCK_PATH.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"another self-evolve scheduler run is active ({LOCK_PATH}, age={int(age)}s)")
    fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat(timespec="seconds")}))


def release_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        log.exception("failed to release lock")


def choose_command(command: str, now: datetime) -> tuple[str | None, str]:
    command = command.strip()
    if command and command != "auto":
        return command, "explicit"
    chosen = DEFAULT_PLAN.get(now.weekday())
    if not chosen:
        return None, "weekend skip"
    return chosen, "weekly rotation"


def within_market_window(now: datetime) -> bool:
    # MarketBrief is scheduled hourly 08:00-22:00. Phase 2a runs after the last
    # brief to avoid competing for MCPs / the LLM backend.
    return 8 <= now.hour < 23


def cooldown_allows(state: dict[str, Any], command_key: str, now: datetime, min_hours: int) -> tuple[bool, str]:
    last = (state.get("last_success") or {}).get(command_key)
    if not last:
        return True, "never ran"
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True, "bad last timestamp"
    elapsed = now - last_dt
    if elapsed >= timedelta(hours=min_hours):
        return True, f"last ran {elapsed} ago"
    return False, f"cooldown active: last ran {elapsed} ago"


def pending_ids() -> set[str]:
    sys.path.insert(0, str(SCRIPTS_DIR))
    import selfevolve  # noqa: WPS433

    return {str(p.get("id")) for p in selfevolve.list_pending_proposals(max_age_days=90)}


async def run_selfevolve_command(command_text: str) -> RunResult:
    sys.path.insert(0, str(SCRIPTS_DIR))
    import listen_weixin  # noqa: WPS433
    import selfevolve  # noqa: WPS433

    started = datetime.now().astimezone().isoformat(timespec="seconds")
    cmd_key = command_text.split()[0].lower()
    handler = listen_weixin.COMMANDS.get(cmd_key)
    if handler is None:
        raise ValueError(f"unknown command: {cmd_key}")
    if cmd_key not in listen_weixin.SELF_EVOLVE_COMMANDS:
        raise ValueError(f"not a self-evolve command: {cmd_key}")

    reply = await handler(command_text)
    cleaned, payload = selfevolve.extract_proposals_block(reply)
    proposal_files: list[Path] = []
    emitted_json = payload is not None
    if payload is not None:
        proposal_files = selfevolve.save_proposals(cmd_key.lstrip("/"), payload)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S EDT", time.localtime())
    out_path = SCRIPTS_DIR / f"last_{cmd_key.lstrip('/')}.md"
    out_path.write_text(
        f"# Last `{cmd_key}` output ({stamp})\n\n"
        f"Triggered by: `selfevolve_scheduler.py {command_text}`\n\n"
        f"---\n\n{cleaned}\n",
        encoding="utf-8",
    )
    finished = datetime.now().astimezone().isoformat(timespec="seconds")
    return RunResult(command_text, cleaned, proposal_files, emitted_json, started, finished)


def summarize(result: RunResult, before: set[str], after: set[str]) -> str:
    new_ids = sorted(after - before)
    lines = [
        "## Self-evolve scheduler",
        "",
        f"- command: `{result.command_text}`",
        f"- status: done",
        f"- proposals emitted this run: {len(result.proposal_files)}",
        f"- pending proposals total: {len(after)}",
        f"- output saved: `last_{result.command_text.split()[0].lstrip('/')}.md`",
    ]
    if not result.emitted_json:
        lines.append("- warning: no parseable `<proposals>` JSON block")
    if new_ids:
        lines.append("")
        lines.append("### New pending proposals")
        for prop_id in new_ids[:8]:
            lines.append(f"- `{prop_id}`")
        if len(new_ids) > 8:
            lines.append(f"- ... plus {len(new_ids) - 8} more")
        lines.append("")
        lines.append("Review in WeChat with `/proposals`, `/show <id>`, then `/apply` or `/reject`.")
    return "\n".join(lines)


def push_summary(message: str) -> None:
    push_tool = SCRIPTS_DIR / "push_weixin.py"
    if not push_tool.exists():
        log.warning("push skipped: %s missing", push_tool)
        return
    proc = subprocess.run(
        [sys.executable, str(push_tool), "--message", message],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    for line in proc.stdout.splitlines():
        log.info("push: %s", line)
    if proc.returncode != 0:
        log.warning("push_weixin.py exited %s", proc.returncode)


def record_run(state: dict[str, Any], command_key: str, status: str, detail: dict[str, Any]) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    state.setdefault("runs", []).append({"at": now, "command": command_key, "status": status, **detail})
    state["runs"] = state["runs"][-100:]
    if status == "success":
        state.setdefault("last_success", {})[command_key] = now
    save_state(state)


async def async_main(args: argparse.Namespace) -> int:
    now = datetime.now().astimezone()
    command_text, reason = choose_command(args.command, now)
    if not command_text:
        log.info("skip: %s", reason)
        return 0
    command_key = command_text.split()[0].lower()

    if within_market_window(now) and not args.force:
        log.info("skip: market-brief window active (%s); use --force to override", now.strftime("%H:%M"))
        return 0

    state = load_state()
    allowed, cooldown_reason = cooldown_allows(state, command_key, now, args.min_interval_hours)
    if not allowed and not args.force:
        log.info("skip: %s", cooldown_reason)
        return 0

    if args.dry_run:
        print(f"DRY_RUN: would run {command_text} ({reason}; {cooldown_reason})")
        return 0

    prepare_llm_env()
    before = pending_ids()
    log.info("running %s (%s; %s)", command_text, reason, cooldown_reason)
    result = await run_selfevolve_command(command_text)
    after = pending_ids()
    summary = summarize(result, before, after)
    log.info("summary:\n%s", summary)

    should_push = args.push_always or bool(after - before) or not result.emitted_json
    if should_push and not args.no_push:
        push_summary(summary)

    record_run(
        state,
        command_key,
        "success",
        {
            "command_text": command_text,
            "proposal_count": len(result.proposal_files),
            "pending_total": len(after),
            "pushed": should_push and not args.no_push,
        },
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="auto", help="auto or explicit command like '/reflect 7d'")
    parser.add_argument("--force", action="store_true", help="ignore market-window and cooldown checks")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-push", action="store_true", help="do not send WeChat summary")
    parser.add_argument("--push-always", action="store_true", help="push summary even when no new proposals")
    parser.add_argument("--min-interval-hours", type=int, default=DEFAULT_MIN_INTERVAL_HOURS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        acquire_lock(force=args.force)
    except Exception as exc:
        log.warning("lock unavailable: %s", exc)
        return 0
    try:
        return asyncio.run(async_main(args))
    except Exception as exc:
        log.exception("scheduler failed")
        state = load_state()
        record_run(state, args.command, "error", {"error": str(exc)[:500]})
        if not args.no_push:
            try:
                push_summary(f"## Self-evolve scheduler\n\n- status: error\n- error: {exc}")
            except Exception:
                log.exception("failed to push error summary")
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
