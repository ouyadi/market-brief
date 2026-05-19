"""Download and OCR Discord image attachments for market-brief.

This utility is intentionally usable outside Claude/Codex:

  1. Pull recent messages from the local discord-selfbot MCP.
  2. Extract image attachments from those messages.
  3. Cache images under ~/Scripts/market-brief/vision_cache/.
  4. Run an optional OCR backend (pytesseract when installed).
  5. Emit JSONL + markdown that the brief generator can read.

If no OCR backend is available, the worker still records local image paths and
metadata. That makes the "needs vision" backlog durable instead of disappearing
inside a transient MCP result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from PIL import Image


DEFAULT_MCP_URL = "http://127.0.0.1:6280/mcp"
DEFAULT_CACHE_DIR = Path.home() / "Scripts" / "market-brief" / "vision_cache"
DEFAULT_TESSDATA_DIR = Path.home() / "Scripts" / "market-brief" / "tessdata"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TICKER_RE = re.compile(r"(?<![A-Z0-9])\$?([A-Z]{1,5})(?![A-Z0-9])")
COMMON_WORDS = {
    "A", "I", "AM", "PM", "AI", "CEO", "CFO", "USD", "EDT", "EST", "NYSE",
    "NASDAQ", "ETF", "EPS", "IV", "OI", "P", "L", "CALL", "PUT", "BUY", "SELL",
}


@dataclass
class AttachmentTask:
    url: str
    channel_id: str = ""
    channel_name: str = ""
    message_id: str = ""
    timestamp: str = ""
    author: str = ""
    caption: str = ""
    filename: str = ""


@dataclass
class OcrResult:
    url_hash: str
    attachment_url: str
    image_path: str
    source: dict[str, str]
    filename: str
    width: int | None
    height: int | None
    bytes: int
    mime: str
    ocr_backend: str
    ocr_text: str
    ocr_error: str
    tickers: list[str]
    created_at: str


class McpClient:
    """Tiny Streamable HTTP MCP client for the local discord-selfbot server."""

    def __init__(self, url: str, timeout: int = 30) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 1

    def initialize(self) -> None:
        result, headers = self._post(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "discord-image-ocr", "version": "0.1"},
            },
        )
        self.session_id = headers.get("mcp-session-id") or self.session_id
        if not isinstance(result, dict):
            raise RuntimeError("MCP initialize did not return an object")
        # FastMCP accepts calls without this notification in practice, but send it
        # to keep the handshake well-formed for stricter servers.
        self._notify("notifications/initialized", {})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self.session_id is None:
            self.initialize()
        result, _ = self._post("tools/call", {"name": name, "arguments": arguments})
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        if isinstance(result, dict) and "content" in result:
            return _decode_tool_content(result["content"])
        return result

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    def _post(self, method: str, params: dict[str, Any]) -> tuple[Any, dict[str, str]]:
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response = requests.post(
            self.url,
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        response.encoding = "utf-8"
        data = _parse_jsonrpc_response(response.text)
        if "error" in data:
            raise RuntimeError(f"MCP {method} failed: {data['error']}")
        return data.get("result"), response.headers

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            requests.post(
                self.url,
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException:
            pass


def _parse_jsonrpc_response(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    data_lines: list[str] = []
    capturing = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("data:"):
            capturing = True
            data_lines.append(line[5:].strip())
        elif data_lines and not line:
            break
        elif capturing:
            # FastMCP may stream a very large JSON-RPC envelope as a single SSE
            # data event that contains raw CR/LF breaks. Treat non-prefixed
            # lines before the blank event separator as continuation chunks.
            data_lines.append(line)
    if data_lines:
        return json.loads("".join(data_lines))
    raise RuntimeError(f"Could not parse MCP response: {text[:200]}")


def _decode_tool_content(content: Any) -> Any:
    if not isinstance(content, list) or not content:
        return content
    if len(content) == 1 and isinstance(content[0], dict):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return content


def load_messages_from_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("messages", "result", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    raise ValueError(f"{path} does not look like a Discord messages JSON file")


def read_channel_messages(
    mcp_url: str,
    channel_id: str,
    limit: int,
    after_id: str | None = None,
    before_id: str | None = None,
) -> list[dict[str, Any]]:
    client = McpClient(mcp_url)
    args: dict[str, Any] = {"channel_id": channel_id, "limit": limit}
    if after_id:
        args["after_id"] = after_id
    if before_id:
        args["before_id"] = before_id
    result = client.call_tool("read_channel_messages", args)
    if isinstance(result, dict) and isinstance(result.get("result"), list):
        result = result["result"]
    if isinstance(result, str):
        raise RuntimeError(result)
    if not isinstance(result, list):
        raise RuntimeError(f"read_channel_messages returned {type(result).__name__}")
    return result


def extract_attachment_tasks(messages: Iterable[dict[str, Any]]) -> list[AttachmentTask]:
    tasks: list[AttachmentTask] = []
    for message in messages:
        attachments = message.get("attachments") or []
        if not isinstance(attachments, list):
            continue
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            url = str(attachment.get("url") or "")
            filename = str(attachment.get("filename") or "")
            if not url:
                continue
            if not _looks_like_image(url, filename):
                continue
            tasks.append(
                AttachmentTask(
                    url=url,
                    channel_id=str(message.get("channel_id") or ""),
                    message_id=str(message.get("id") or ""),
                    timestamp=str(message.get("timestamp") or ""),
                    author=str(
                        message.get("author_display")
                        or message.get("author_name")
                        or message.get("author_id")
                        or ""
                    ),
                    caption=str(message.get("content") or ""),
                    filename=filename,
                )
            )
    return tasks


def _looks_like_image(url: str, filename: str) -> bool:
    suffix = Path(filename or urlparse(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return True
    lowered = url.lower()
    return any(ext in lowered for ext in IMAGE_EXTENSIONS)


def filter_since(tasks: Iterable[AttachmentTask], since_hours: float | None) -> list[AttachmentTask]:
    if since_hours is None:
        return list(tasks)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    out: list[AttachmentTask] = []
    for task in tasks:
        try:
            ts = datetime.fromisoformat(task.timestamp.replace("Z", "+00:00"))
        except ValueError:
            out.append(task)
            continue
        if ts >= cutoff:
            out.append(task)
    return out


def choose_budget(tasks: list[AttachmentTask], max_images: int) -> list[AttachmentTask]:
    """Newest-first budget with first/last sampling within a bursty message."""
    if max_images <= 0 or len(tasks) <= max_images:
        return tasks

    by_message: dict[str, list[AttachmentTask]] = {}
    for task in tasks:
        key = task.message_id or task.url
        by_message.setdefault(key, []).append(task)

    selected: list[AttachmentTask] = []
    for _, group in sorted(by_message.items(), key=lambda kv: _sort_time(kv[1][0]), reverse=True):
        if len(group) > 6:
            sampled = group[:3] + group[-3:]
        else:
            sampled = group
        for task in sampled:
            if len(selected) >= max_images:
                return selected
            selected.append(task)
    return selected


def _sort_time(task: AttachmentTask) -> float:
    try:
        return datetime.fromisoformat(task.timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def process_task(task: AttachmentTask, cache_dir: Path, backend: str) -> OcrResult:
    image_dir = cache_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.sha256(task.url.encode("utf-8")).hexdigest()[:16]
    suffix = Path(task.filename or urlparse(task.url).path).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".jpg"
    image_path = image_dir / f"{url_hash}{suffix}"

    if not image_path.exists():
        response = requests.get(task.url, timeout=30)
        response.raise_for_status()
        image_path.write_bytes(response.content)

    width: int | None = None
    height: int | None = None
    mime = mimetypes.guess_type(str(image_path))[0] or ""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            if not mime:
                mime = Image.MIME.get(img.format, "")
    except Exception:
        pass

    ocr_backend, ocr_text, ocr_error = run_ocr(image_path, backend)
    combined_text = "\n".join(x for x in (task.caption, ocr_text) if x)
    tickers = extract_tickers(combined_text)
    return OcrResult(
        url_hash=url_hash,
        attachment_url=task.url,
        image_path=str(image_path),
        source={
            "channel_id": task.channel_id,
            "channel_name": task.channel_name,
            "message_id": task.message_id,
            "timestamp": task.timestamp,
            "author": task.author,
            "caption": task.caption,
        },
        filename=task.filename,
        width=width,
        height=height,
        bytes=image_path.stat().st_size,
        mime=mime,
        ocr_backend=ocr_backend,
        ocr_text=ocr_text,
        ocr_error=ocr_error,
        tickers=tickers,
        created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    )


def run_ocr(image_path: Path, backend: str) -> tuple[str, str, str]:
    if backend == "none":
        return "none", "", "OCR disabled; image cached for manual/vision review"
    if backend not in {"auto", "tesseract"}:
        return backend, "", f"Unknown OCR backend: {backend}"
    try:
        import pytesseract  # type: ignore
    except ImportError:
        if backend == "tesseract":
            return "tesseract", "", "pytesseract is not installed"
        return "none", "", "No OCR backend installed (pytesseract/tesseract unavailable)"

    try:
        tesseract_cmd = shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        if Path(tesseract_cmd).exists():
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        lang = "eng"
        config = ""
        if DEFAULT_TESSDATA_DIR.exists():
            config = f"--tessdata-dir {DEFAULT_TESSDATA_DIR}"
            if (DEFAULT_TESSDATA_DIR / "chi_sim.traineddata").exists():
                lang = "chi_sim+eng"
        with Image.open(image_path) as img:
            text = pytesseract.image_to_string(img, lang=lang, config=config)
        return "tesseract", text.strip(), ""
    except Exception as exc:
        return "tesseract", "", str(exc)


def extract_tickers(text: str) -> list[str]:
    tickers: set[str] = set()
    for match in TICKER_RE.finditer(text.upper()):
        ticker = match.group(1)
        if ticker in COMMON_WORDS:
            continue
        if len(ticker) == 1 and f"${ticker}" not in text.upper():
            continue
        tickers.add(ticker)
    return sorted(tickers)


def write_jsonl(path: Path, results: list[OcrResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


def write_markdown(path: Path, results: list[OcrResult], backend: str, scan_errors: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Discord Image OCR Batch",
        "",
        f"> generated_at: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        f"> requested_backend: {backend}",
        f"> images: {len(results)}",
        "",
        "## Summary",
        "",
        f"- with_tickers: {sum(1 for r in results if r.tickers)}",
        f"- needs_vision: {sum(1 for r in results if not r.ocr_text)}",
        f"- ocr_backends: {', '.join(sorted({r.ocr_backend for r in results})) if results else 'none'}",
        "",
    ]
    if scan_errors:
        lines.extend(["## Scan Errors", ""])
        for error in scan_errors:
            lines.append(f"- {error}")
        lines.append("")
    for i, result in enumerate(results, 1):
        source = result.source
        title_bits = [
            source.get("author") or "unknown",
            source.get("timestamp") or "",
        ]
        lines.extend([
            f"## {i}. {' | '.join(x for x in title_bits if x)}",
            "",
            f"- channel_id: `{source.get('channel_id', '')}`",
            f"- message_id: `{source.get('message_id', '')}`",
            f"- image: `{result.image_path}` ({result.width}x{result.height}, {result.bytes} bytes)",
            f"- attachment_url: {result.attachment_url}",
            f"- tickers: {', '.join('$' + t for t in result.tickers) if result.tickers else '(none)'}",
        ])
        caption = (source.get("caption") or "").strip()
        if caption:
            lines.append(f"- caption: {caption[:240]}")
        if result.ocr_text:
            lines.extend(["", "```text", result.ocr_text[:4000], "```"])
        else:
            lines.append(f"- status: needs_vision ({result.ocr_error})")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def default_output_paths(cache_dir: Path) -> tuple[Path, Path]:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir = cache_dir / "runs"
    return out_dir / f"{stamp}.jsonl", out_dir / f"{stamp}.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", default=os.environ.get("DISCORD_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument("--channel-id", action="append", default=[], help="Discord channel id to scan")
    parser.add_argument("--limit", type=int, default=80, help="Messages per channel (1-200)")
    parser.add_argument("--after-id")
    parser.add_argument("--before-id")
    parser.add_argument("--since-hours", type=float, default=None)
    parser.add_argument("--message-json", action="append", type=Path, default=[], help="JSON file containing messages")
    parser.add_argument("--url", action="append", default=[], help="Direct image URL")
    parser.add_argument("--label", default="", help="Label/caption for direct --url inputs")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--max-images", type=int, default=15)
    parser.add_argument("--backend", choices=["auto", "none", "tesseract"], default="auto")
    parser.add_argument("--jsonl", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--print-markdown", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1 or args.limit > 200:
        raise SystemExit("--limit must be between 1 and 200")

    tasks: list[AttachmentTask] = []
    scan_errors: list[str] = []
    for channel_id in args.channel_id:
        try:
            messages = read_channel_messages(
                args.mcp_url,
                channel_id=channel_id,
                limit=args.limit,
                after_id=args.after_id,
                before_id=args.before_id,
            )
            tasks.extend(extract_attachment_tasks(messages))
        except Exception as exc:
            scan_errors.append(f"channel {channel_id}: {exc}")
    for path in args.message_json:
        tasks.extend(extract_attachment_tasks(load_messages_from_json(path)))
    for url in args.url:
        tasks.append(AttachmentTask(url=url, caption=args.label, filename=Path(urlparse(url).path).name))

    tasks = filter_since(tasks, args.since_hours)
    tasks.sort(key=_sort_time, reverse=True)
    tasks = choose_budget(tasks, args.max_images)

    results: list[OcrResult] = []
    for task in tasks:
        try:
            results.append(process_task(task, args.cache_dir, args.backend))
        except Exception as exc:
            url_hash = hashlib.sha256(task.url.encode("utf-8")).hexdigest()[:16]
            results.append(
                OcrResult(
                    url_hash=url_hash,
                    attachment_url=task.url,
                    image_path="",
                    source={
                        "channel_id": task.channel_id,
                        "channel_name": task.channel_name,
                        "message_id": task.message_id,
                        "timestamp": task.timestamp,
                        "author": task.author,
                        "caption": task.caption,
                    },
                    filename=task.filename,
                    width=None,
                    height=None,
                    bytes=0,
                    mime="",
                    ocr_backend=args.backend,
                    ocr_text="",
                    ocr_error=f"download/process failed: {exc}",
                    tickers=extract_tickers(task.caption),
                    created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                )
            )

    jsonl_path, markdown_path = default_output_paths(args.cache_dir)
    jsonl_path = args.jsonl or jsonl_path
    markdown_path = args.markdown or markdown_path
    write_jsonl(jsonl_path, results)
    write_markdown(markdown_path, results, args.backend, scan_errors)

    if args.print_markdown:
        sys.stdout.write(markdown_path.read_text(encoding="utf-8"))
    else:
        print(f"JSONL_WRITTEN: {jsonl_path}")
        print(f"MARKDOWN_WRITTEN: {markdown_path}")
        print(f"IMAGES_PROCESSED: {len(results)}")
        print(f"NEEDS_VISION: {sum(1 for r in results if not r.ocr_text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
