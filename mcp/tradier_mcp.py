"""tradier_mcp.py -- HTTP MCP server wrapping Tradier brokerage REST + Stream.

Tradier prod (real-time): bulk quotes, 1-min OHLCV history (15d), daily history,
and a long-lived streaming consumer that fans events out to Redis pub/sub so
multiple consumers (alphalens Next.js SSE routes, market-brief workers, etc.)
can subscribe without each holding their own connection.

REST tools:
  get_quotes(symbols)               bulk quote lookup, comma-sep symbols
  get_timesales(symbol, interval)   intraday OHLCV bars (1min/5min/15min, 15d window)
  get_history(symbol, interval)     daily/weekly/monthly historical bars
  stream_status()                   bg-consumer health snapshot
  set_stream_symbols(symbols)       resubscribe stream universe at runtime

Background stream consumer (lives in same process, started on launch):
  Tradier sessionid → HTTP chunked NDJSON → Redis pub/sub channels:
    tradier:trade:<SYMBOL>    individual trade prints
    tradier:quote:<SYMBOL>    bid/ask updates
    tradier:summary:<SYMBOL>  o/h/l/prev_close snapshot
    tradier:timesale:<SYMBOL> Tradier-aggregated 1m bars
  Reconnects on disconnect (5s backoff, fresh sessionid).
  Runs whether market is open or not — connection survives off-hours but
  most event types only fire during RTH.

Runs on 127.0.0.1:3036/mcp by default. Override port via TRADIER_MCP_PORT.
Requires TRADIER_API_TOKEN env var. Optional REDIS_URL (defaults to
redis://localhost:6379/0).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp
import redis.asyncio as aioredis
from mcp.server.fastmcp import FastMCP

# ── Logging + dirs ────────────────────────────────────────────────────────
LOG_DIR = (
    Path(os.environ.get("TRADIER_MCP_DIR") or (Path.home() / "tradier-mcp")) / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "tradier_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("tradier")

# ── Config ────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("TRADIER_API_TOKEN", "")
REST_BASE = os.environ.get("TRADIER_REST_BASE", "https://api.tradier.com/v1")
STREAM_BASE = os.environ.get("TRADIER_STREAM_BASE", "https://stream.tradier.com/v1")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Universe to start streaming on launch. Set via env or rely on tide indices.
DEFAULT_STREAM_SYMBOLS = os.environ.get(
    "TRADIER_STREAM_SYMBOLS", "SPY,QQQ,IWM,DIA,TLT,HYG,VIX"
).split(",")
STREAM_FILTER = os.environ.get("TRADIER_STREAM_FILTER", "trade,quote,summary,timesale")

HTTP_TIMEOUT_S = 15
RATE_LIMIT_INTERVAL_S = 60.0 / 120  # Tradier: 120 req/min on prod
_last_rest_call = 0.0
_rest_lock = asyncio.Lock()

mcp = FastMCP(
    "tradier",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("TRADIER_MCP_PORT", "3036")),
)

# ── Stream state ──────────────────────────────────────────────────────────
class StreamState:
    """Mutable shared state for the background stream consumer."""

    def __init__(self) -> None:
        self.symbols: set[str] = set(s.strip().upper() for s in DEFAULT_STREAM_SYMBOLS if s.strip())
        self.connected: bool = False
        self.sessionid: str | None = None
        self.last_event_ts: float = 0.0
        self.events_total: int = 0
        self.events_by_type: dict[str, int] = {}
        self.last_error: str | None = None
        self.last_connect_ts: float = 0.0
        self.reconnect_event = asyncio.Event()  # set this to force restart

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "symbols": sorted(self.symbols),
            "symbol_count": len(self.symbols),
            "sessionid_present": self.sessionid is not None,
            "events_total": self.events_total,
            "events_by_type": dict(self.events_by_type),
            "last_event_age_sec": (time.time() - self.last_event_ts) if self.last_event_ts else None,
            "last_connect_age_sec": (time.time() - self.last_connect_ts) if self.last_connect_ts else None,
            "last_error": self.last_error,
        }


STATE = StreamState()
_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


# ── REST helpers ──────────────────────────────────────────────────────────
async def _tradier_request(path: str, params: dict | None = None) -> dict:
    """GET a Tradier REST endpoint, return parsed JSON.

    Enforces a global ~120 req/min rate limit across all REST tools.
    """
    if not TOKEN:
        raise RuntimeError("TRADIER_API_TOKEN env var is not set")

    global _last_rest_call
    async with _rest_lock:
        wait = RATE_LIMIT_INTERVAL_S - (time.time() - _last_rest_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_rest_call = time.time()

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = f"{REST_BASE}/{path.lstrip('/')}"
        async with session.get(url, params=params, headers=headers) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"Tradier {path} → {r.status}: {text[:200]}")
            return json.loads(text)


# ── REST tools ────────────────────────────────────────────────────────────
@mcp.tool()
async def get_quotes(symbols: str) -> dict:
    """Bulk quote lookup. `symbols` is a comma-separated string (no spaces),
    e.g. 'SPY,QQQ,AAPL'. Returns normalized list of quote objects.

    Use this for /signals index strip, /tickers detail header, and the
    Heatmap snapshot refresh. Tradier permits up to ~1000 symbols per call;
    above that, batch yourself.
    """
    if not symbols.strip():
        return {"success": False, "error": "empty symbols", "quotes": []}

    data = await _tradier_request("markets/quotes", {"symbols": symbols, "greeks": "false"})
    raw = data.get("quotes", {})
    if raw == "null" or raw is None:
        return {"success": True, "count": 0, "quotes": []}

    quote_field = raw.get("quote", []) if isinstance(raw, dict) else []
    if isinstance(quote_field, dict):  # Tradier returns single dict when only one symbol
        quote_field = [quote_field]

    out = []
    for q in quote_field:
        out.append(
            {
                "symbol": q.get("symbol"),
                "description": q.get("description"),
                "type": q.get("type"),
                "last": q.get("last"),
                "change": q.get("change"),
                "change_percentage": q.get("change_percentage"),
                "volume": q.get("volume"),
                "average_volume": q.get("average_volume"),
                "last_volume": q.get("last_volume"),
                "trade_date": q.get("trade_date"),
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "close": q.get("close"),
                "prev_close": q.get("prevclose"),
                "week_52_high": q.get("week_52_high"),
                "week_52_low": q.get("week_52_low"),
                "bid": q.get("bid"),
                "ask": q.get("ask"),
                "bidsize": q.get("bidsize"),
                "asksize": q.get("asksize"),
                "exch": q.get("exch"),
            }
        )
    return {"success": True, "count": len(out), "quotes": out}


@mcp.tool()
async def get_timesales(
    symbol: str,
    interval: str = "1min",
    start: str | None = None,
    end: str | None = None,
    session_filter: str = "all",
) -> dict:
    """Intraday OHLCV bars for one symbol.

    interval: '1min' | '5min' | '15min'  (Tradier max history depth: 5 trading
              days for 1min, 20 days for 5min, 40 days for 15min — approximately)
    start/end: 'YYYY-MM-DD HH:MM' ET. If both omitted, returns today's session.
    session_filter: 'open' | 'closed' | 'all'  ('all' includes pre/post market)

    Returns array of {time, timestamp, price, open, high, low, close, volume, vwap}.
    `price` is Tradier's typical-price (HLC/3). vwap is Tradier-computed.
    """
    params = {"symbol": symbol.upper(), "interval": interval, "session_filter": session_filter}
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    data = await _tradier_request("markets/timesales", params)
    series = data.get("series", {})
    if series == "null" or series is None:
        return {"success": True, "symbol": symbol.upper(), "interval": interval, "count": 0, "bars": []}

    bars_raw = series.get("data", []) if isinstance(series, dict) else []
    if isinstance(bars_raw, dict):
        bars_raw = [bars_raw]

    bars = []
    for b in bars_raw:
        bars.append(
            {
                "time": b.get("time"),
                "timestamp": b.get("timestamp"),
                "price": b.get("price"),
                "open": b.get("open"),
                "high": b.get("high"),
                "low": b.get("low"),
                "close": b.get("close"),
                "volume": b.get("volume"),
                "vwap": b.get("vwap"),
            }
        )
    return {
        "success": True,
        "symbol": symbol.upper(),
        "interval": interval,
        "session_filter": session_filter,
        "count": len(bars),
        "bars": bars,
    }


@mcp.tool()
async def get_history(
    symbol: str,
    interval: str = "daily",
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Daily/weekly/monthly OHLCV history.

    interval: 'daily' | 'weekly' | 'monthly'
    start/end: 'YYYY-MM-DD'. If omitted, Tradier returns a default window
               (usually a few years for daily).

    Returns array of {date, open, high, low, close, volume}.
    Use this for: 30D sparklines on heatmap tiles, multi-day Tide trend.
    """
    params = {"symbol": symbol.upper(), "interval": interval}
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    data = await _tradier_request("markets/history", params)
    history = data.get("history", {})
    if history == "null" or history is None:
        return {"success": True, "symbol": symbol.upper(), "interval": interval, "count": 0, "bars": []}

    bars_raw = history.get("day", []) if isinstance(history, dict) else []
    if isinstance(bars_raw, dict):
        bars_raw = [bars_raw]

    return {
        "success": True,
        "symbol": symbol.upper(),
        "interval": interval,
        "count": len(bars_raw),
        "bars": bars_raw,
    }


# ── Stream tools (admin / introspection) ──────────────────────────────────
@mcp.tool()
async def stream_status() -> dict:
    """Snapshot of the background stream consumer's health.

    Returns connection state, current subscribed symbol list, event totals
    by type, last event age, and last error if any. Use to diagnose why
    Tide / Heatmap UI is not getting live updates.
    """
    return STATE.snapshot()


@mcp.tool()
async def set_stream_symbols(symbols: str) -> dict:
    """Resubscribe the stream consumer to a new symbol universe.

    `symbols`: comma-separated, e.g. 'SPY,QQQ,IWM,AAPL,MSFT,...'. Replaces
    the current subscription. The consumer will tear down the existing
    connection and reconnect within ~5s. Use to dynamically expand to
    S&P 500 for Heatmap, or shrink back to a small index set after-hours.
    """
    new_set = {s.strip().upper() for s in symbols.split(",") if s.strip()}
    if not new_set:
        return {"success": False, "error": "empty symbol list ignored"}
    old = sorted(STATE.symbols)
    STATE.symbols = new_set
    STATE.reconnect_event.set()
    log.info("stream symbols changed: %d → %d", len(old), len(new_set))
    return {
        "success": True,
        "old_count": len(old),
        "new_count": len(new_set),
        "symbols": sorted(new_set),
    }


# ── Background stream consumer ────────────────────────────────────────────
async def _create_session() -> tuple[str, str]:
    """POST /markets/events/session → (sessionid, stream_url).

    Tradier's response includes both the sessionid and the canonical stream
    URL to connect to. We use that URL directly instead of hardcoding STREAM_BASE.
    """
    if not TOKEN:
        raise RuntimeError("TRADIER_API_TOKEN env var is not set")
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = f"{REST_BASE}/markets/events/session"
        async with session.post(url, headers=headers) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"create_session → {r.status}: {text[:200]}")
            data = json.loads(text)
            stream_info = data.get("stream", {})
            sid = stream_info.get("sessionid")
            url_from_resp = stream_info.get("url") or f"{STREAM_BASE}/markets/events"
            if not sid:
                raise RuntimeError(f"no sessionid in response: {text[:200]}")
            return sid, url_from_resp


async def _publish_event(redis: aioredis.Redis, evt: dict) -> None:
    """Map a Tradier stream event to a Redis pub channel + counter."""
    etype = evt.get("type", "unknown")
    sym = evt.get("symbol", "_") or "_"
    channel = f"tradier:{etype}:{sym}"
    try:
        await redis.publish(channel, json.dumps(evt, separators=(",", ":")))
    except Exception as e:
        log.warning("redis publish %s failed: %s", channel, e)
        return
    STATE.events_total += 1
    STATE.events_by_type[etype] = STATE.events_by_type.get(etype, 0) + 1
    STATE.last_event_ts = time.time()


async def _stream_once(redis: aioredis.Redis) -> None:
    """One streaming connection lifecycle: create session, open SSE, pump events."""
    sid, url = await _create_session()
    STATE.sessionid = sid
    STATE.last_connect_ts = time.time()
    symbols = ",".join(sorted(STATE.symbols))
    log.info(
        "stream connecting, sessionid=%s url=%s symbols=%d",
        sid[:8] + "...", url, len(STATE.symbols),
    )

    # Long-lived chunked HTTP — no aiohttp total timeout. Accept JSON (Tradier
    # rejects application/x-ndjson with 406 even though they ship NDJSON).
    timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}
    params = {
        "sessionid": sid,
        "symbols": symbols,
        "filter": STREAM_FILTER,
        "linebreak": "true",
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params, headers=headers) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f"stream GET → {r.status}: {text[:200]}")
            STATE.connected = True
            STATE.last_error = None
            log.info("stream connected")

            async for raw_line in r.content:
                if STATE.reconnect_event.is_set():
                    log.info("reconnect_event set → tearing down current stream")
                    return
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await _publish_event(redis, evt)


async def stream_consumer() -> None:
    """Outer loop: keep a stream connection alive, reconnect on any failure."""
    backoff = 5.0
    redis = await get_redis()
    while True:
        STATE.reconnect_event.clear()
        try:
            await _stream_once(redis)
            # Clean exit (e.g. reconnect_event set) → loop immediately
            STATE.connected = False
            backoff = 5.0
        except Exception as e:
            STATE.connected = False
            STATE.last_error = f"{type(e).__name__}: {e}"
            log.warning("stream loop error: %s — sleep %.0fs", STATE.last_error, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 60.0)


# ── Server lifecycle: spawn stream consumer on first request ──────────────
_stream_task: asyncio.Task | None = None


@mcp.tool()
async def ensure_stream_running() -> dict:
    """Idempotently start (or report status of) the background stream consumer.

    The consumer is also auto-started on first non-trivial REST call, but you
    can call this explicitly after a restart to kick it off without waiting.
    """
    global _stream_task
    if _stream_task is None or _stream_task.done():
        _stream_task = asyncio.create_task(stream_consumer())
        log.info("stream consumer task spawned")
        return {"started": True, "state": STATE.snapshot()}
    return {"started": False, "already_running": True, "state": STATE.snapshot()}


if __name__ == "__main__":
    log.info(
        "tradier MCP starting on http://127.0.0.1:%s/mcp (token=%s, redis=%s)",
        mcp.settings.port,
        "set" if TOKEN else "MISSING",
        REDIS_URL,
    )
    # Stream consumer is NOT auto-spawned at boot — fastmcp's transport
    # owns the event loop and we don't have a clean lifespan hook. Call
    # the `ensure_stream_running` MCP tool once from any client (e.g.
    # alphalens does this on first /api/stream/quotes hit) and the
    # consumer keeps running for the lifetime of this process.
    mcp.run(transport="streamable-http")
