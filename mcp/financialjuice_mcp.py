"""financialjuice_mcp.py -- HTTP MCP server for real-time financial wire.

FinancialJuice (financialjuice.com) is a real-time financial+geopolitical
news aggregator. Their public RSS feed `/feed.ashx?xy=rss` returns ~100 most
recent headlines with timestamps. No auth required.

This MCP:
- Polls the RSS feed every POLL_INTERVAL_S (default 300 = 5 min)
- Maintains a deduped local cache at ~/financialjuice-mcp/cache.jsonl
  so queries can look back further than the ~100 items the feed snapshot
  exposes at any moment (typically 6-12h depending on news velocity)
- Auto-tags each headline with lightweight regex categories
  (geopolitics / fed / macro / trump / earnings / crypto / tickers / ipo)
  so callers can filter without re-implementing classification per tool

Tools exposed:
  list_headlines(since, limit, query, tag) -- main lookup
  get_tagged(tag, limit)                    -- one-category shortcut
  get_for_ticker(ticker, since)             -- headlines mentioning $TICKER
  cache_status()                            -- debug / health

Use cases for market-brief:
  - Macro/geo event-line (Iran, Trump trade, Fed remarks) as raw news
    complement to Polymarket's market-implied probabilities
  - Real-time catalyst surfacing for watchlist tickers
  - Pre-market sweep of overnight wire

Run as HTTP MCP on 127.0.0.1:3034/mcp by default. Override via
FINANCIALJUICE_MCP_PORT env var.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(os.environ.get("FINANCIALJUICE_MCP_DIR") or (Path.home() / "financialjuice-mcp"))
LOG_DIR = BASE_DIR / "logs"
CACHE_FILE = BASE_DIR / "cache.jsonl"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "financialjuice_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("financialjuice")

RSS_URL = "https://www.financialjuice.com/feed.ashx?xy=rss"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
POLL_INTERVAL_S = int(os.environ.get("FINANCIALJUICE_POLL_S", "300"))  # 5 min default
HTTP_TIMEOUT_S = 15

mcp = FastMCP(
    "financialjuice",
    host="127.0.0.1",
    port=int(os.environ.get("FINANCIALJUICE_MCP_PORT", "3034")),
)


# ──────────────────────── Lightweight tagger ─────────────────────────


_TAG_PATTERNS: dict[str, re.Pattern] = {
    "fed": re.compile(
        r"\b(fed(eral)?\s*reserve|fomc|powell|jerome\s+powell|rate\s+(cut|hike|hold|decision)|"
        r"\bbps\b|monetary\s+policy|interest\s+rate)",
        re.I,
    ),
    "macro": re.compile(
        r"\b(cpi|ppi|nfp|payroll|jobs?\s+report|unemployment|inflation|gdp|retail\s+sales|"
        r"ism\s+(manufacturing|services)|consumer\s+confidence|pmi)",
        re.I,
    ),
    "trump": re.compile(r"\b(trump|potus|white\s+house|administration|trump\s+admin)\b", re.I),
    "geopolitics": re.compile(
        r"\b(iran|israel|gaza|hamas|ukraine|russia|putin|china|xi\s+jinping|taiwan|"
        r"north\s+korea|kim\s+jong|nato|nuclear|drone|missile|strike|sanctions)\b",
        re.I,
    ),
    "earnings": re.compile(
        r"\b(earnings|q[1-4]\s+(report|results)|revenue\s+beat|revenue\s+miss|eps\s+beat|"
        r"eps\s+miss|guidance|forecast|outlook)\b",
        re.I,
    ),
    "crypto": re.compile(
        r"\b(bitcoin|btc\b|ethereum|eth\b|stablecoin|usdt|usdc|crypto|defi|memecoin|"
        r"binance|coinbase|sec\s+vs)\b",
        re.I,
    ),
    "ipo": re.compile(
        r"\b(ipo|listing|h\s+share|debut|offering|global\s+offering|prospectus)\b", re.I
    ),
    "china": re.compile(
        r"\b(pboc|csrc|shenzhen|shanghai|hong\s+kong|hsi\b|csi\b|hang\s+seng|"
        r"china(?!\s+(?:cabinet|tea|town)))\b",
        re.I,
    ),
}
_TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")


def _classify(title: str) -> dict[str, Any]:
    tags = sorted(t for t, p in _TAG_PATTERNS.items() if p.search(title))
    tickers = sorted(set(_TICKER_RE.findall(title)))
    return {"tags": tags, "tickers": tickers}


# ──────────────────────────── Cache I/O ─────────────────────────────


def _load_cache() -> dict[str, dict]:
    """Return {url: headline} dict. URL is the dedupe key."""
    if not CACHE_FILE.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                    if "url" in item:
                        out[item["url"]] = item
                except Exception:
                    continue
    except Exception:
        log.exception("cache load failed")
    return out


_cache_lock = asyncio.Lock()


async def _append_new(items: list[dict]) -> int:
    """Append items not already in cache. Returns count of new items."""
    async with _cache_lock:
        existing = _load_cache()
        new = [it for it in items if it.get("url") and it["url"] not in existing]
        if not new:
            return 0
        with CACHE_FILE.open("a", encoding="utf-8") as f:
            for it in new:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return len(new)


# ─────────────────────────── RSS fetch ──────────────────────────────


def _parse_rss_date(s: str) -> int | None:
    """Parse RFC 822 ts like 'Sun, 17 May 2026 22:40:49 GMT' to epoch sec."""
    if not s:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%a, %d %b %Y %H:%M:%S",
    ):
        try:
            return int(
                datetime.datetime.strptime(s.strip(), fmt)
                .replace(tzinfo=datetime.timezone.utc)
                .timestamp()
            )
        except ValueError:
            continue
    return None


async def _fetch_rss() -> list[dict]:
    headers = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml"}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(RSS_URL) as r:
            r.raise_for_status()
            text = await r.text()

    items: list[dict] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        log.warning("RSS parse failed: %s", e)
        return items

    # RSS 2.0: rss/channel/item
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        ts = _parse_rss_date(pub) or int(time.time())
        ts_iso = (
            datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        meta = _classify(title)
        items.append({
            "ts": ts,
            "ts_iso": ts_iso,
            "title": title,
            "url": link or f"financialjuice:{ts}:{title[:60]}",  # fallback synthetic id
            "tags": meta["tags"],
            "tickers": meta["tickers"],
        })
    return items


async def _poll_loop() -> None:
    """Background task: poll RSS every POLL_INTERVAL_S, append new items."""
    log.info(
        "poll loop started: every %ds, cache=%s", POLL_INTERVAL_S, CACHE_FILE
    )
    # First poll immediately on startup to warm the cache
    first = True
    while True:
        try:
            items = await _fetch_rss()
            n_new = await _append_new(items)
            log.info(
                "poll: fetched %d items, %d new (first=%s)",
                len(items), n_new, first,
            )
        except Exception:
            log.exception("poll iteration failed")
        first = False
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            log.info("poll loop cancelled")
            raise


_LOOKBACK_RE = re.compile(r"^\s*(\d+)\s*(mo|h|d|w|m)\s*$", re.I)


def _parse_lookback(s: str) -> int | None:
    m = _LOOKBACK_RE.match(s or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    table = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000}
    return n * table[unit]


def _filter(
    items: list[dict],
    since_s: int | None = None,
    query: str = "",
    tag: str = "",
    ticker: str = "",
) -> list[dict]:
    """Apply optional filters in time + query + tag + ticker."""
    now = int(time.time())
    out = []
    ql = query.lower().strip() if query else ""
    tag = tag.strip().lower() if tag else ""
    tk = ticker.strip().upper().lstrip("$") if ticker else ""
    for it in items:
        if since_s is not None and now - it.get("ts", 0) > since_s:
            continue
        if ql and ql not in (it.get("title") or "").lower():
            continue
        if tag and tag not in (it.get("tags") or []):
            continue
        if tk and tk not in (it.get("tickers") or []):
            continue
        out.append(it)
    return out


# ───────────────────────────── Tools ────────────────────────────────


@mcp.tool()
async def list_headlines(
    since: str = "1h",
    limit: int = 30,
    query: str = "",
    tag: str = "",
) -> dict:
    """List FinancialJuice headlines from the local cache, optionally filtered.

    since: lookback as '15m' / '1h' / '6h' / '24h' / '7d'. Default '1h'.
    limit: 1-100. Default 30.
    query: case-insensitive substring filter on title.
    tag: filter by one of {fed, macro, trump, geopolitics, earnings, crypto,
         ipo, china}. Empty = no filter.

    Returns items newest-first.
    """
    secs = _parse_lookback(since)
    if secs is None:
        return {"success": False, "error": f"invalid since: {since!r}"}
    limit = max(1, min(100, limit))
    cache = list(_load_cache().values())
    cache.sort(key=lambda x: x.get("ts", 0), reverse=True)
    filtered = _filter(cache, since_s=secs, query=query, tag=tag)
    log.info(
        "list_headlines since=%s q=%r tag=%r -> %d/%d items",
        since, query, tag, len(filtered), len(cache),
    )
    return {
        "success": True,
        "since": since,
        "cache_total": len(cache),
        "count": min(limit, len(filtered)),
        "headlines": filtered[:limit],
    }


@mcp.tool()
async def get_tagged(tag: str, limit: int = 20, since: str = "6h") -> dict:
    """Shortcut: headlines matching one tag in a window.

    tag: fed | macro | trump | geopolitics | earnings | crypto | ipo | china
    limit: 1-50. since: '15m' / '1h' / '24h' etc. Default '6h'.
    """
    if tag not in _TAG_PATTERNS:
        return {
            "success": False,
            "error": f"unknown tag {tag!r}. Valid: {sorted(_TAG_PATTERNS)}",
        }
    return await list_headlines(since=since, limit=max(1, min(50, limit)), tag=tag)


@mcp.tool()
async def get_for_ticker(ticker: str, since: str = "24h", limit: int = 20) -> dict:
    """Headlines mentioning a specific cashtag ($TICKER).

    Pulls from local cache and filters where the headline title contains
    `$TICKER` literally. Most FinancialJuice items don't carry cashtags
    (the wire-style headlines name companies in prose, not as $-prefixed
    tickers), so misses are common -- treat as best-effort, not exhaustive.
    """
    secs = _parse_lookback(since)
    if secs is None:
        return {"success": False, "error": f"invalid since: {since!r}"}
    limit = max(1, min(50, limit))
    cache = list(_load_cache().values())
    cache.sort(key=lambda x: x.get("ts", 0), reverse=True)
    filtered = _filter(cache, since_s=secs, ticker=ticker)
    return {
        "success": True,
        "ticker": ticker.upper().lstrip("$"),
        "since": since,
        "count": min(limit, len(filtered)),
        "headlines": filtered[:limit],
    }


@mcp.tool()
async def cache_status() -> dict:
    """Debug / health: cache row count + oldest/newest timestamps."""
    cache = list(_load_cache().values())
    if not cache:
        return {
            "success": True,
            "cache_total": 0,
            "note": "cache empty -- daemon may not have polled yet",
        }
    cache.sort(key=lambda x: x.get("ts", 0))
    return {
        "success": True,
        "cache_total": len(cache),
        "oldest_ts_iso": cache[0].get("ts_iso"),
        "newest_ts_iso": cache[-1].get("ts_iso"),
        "newest_titles": [x.get("title", "")[:80] for x in cache[-5:]][::-1],
        "tags_distribution": {
            tag: sum(1 for x in cache if tag in (x.get("tags") or []))
            for tag in sorted(_TAG_PATTERNS)
        },
    }


# ───────────────────────── Entrypoint ───────────────────────────────


async def _wrapper() -> None:
    poll_task = asyncio.create_task(_poll_loop())
    try:
        await asyncio.Event().wait()  # idle forever
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except (asyncio.CancelledError, Exception):
            pass


if __name__ == "__main__":
    log.info(
        "financialjuice MCP starting on http://127.0.0.1:%s/mcp, poll every %ds",
        mcp.settings.port, POLL_INTERVAL_S,
    )
    # FastMCP's mcp.run() blocks. The polling task needs to run in the same
    # asyncio loop. FastMCP exposes a way to add lifespan hooks; the simplest
    # cross-version-safe approach is to start the poll loop on a separate
    # thread that runs its own event loop. That's belt-and-suspenders --
    # FastMCP's HTTP transport runs uvicorn which has its own loop, so we
    # don't conflict.
    import threading

    def _bg_poll():
        asyncio.run(_poll_loop())

    threading.Thread(target=_bg_poll, daemon=True).start()
    mcp.run(transport="streamable-http")
