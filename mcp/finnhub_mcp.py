"""finnhub_mcp.py -- HTTP MCP server exposing Finnhub stock data (free tier).

Finnhub's free tier gives company news, analyst recommendation trends, and an
earnings calendar -- useful complements to the existing news/ratings sources.
Free tier is 60 req/min; we enforce ~1 rps client-side + single concurrency so
a burst of per-ticker calls never trips the limit.

Requires a free API key (register at finnhub.io). Key read from FINNHUB_API_KEY
env only -- never hardcoded. With no key set, tools return success:false.

Tools:
  company_news(ticker, from_date="", to_date="")   default last 24h
  recommendation_trends(ticker)                     analyst buy/hold/sell trend
  earnings_calendar(from_date, to_date)             scheduled earnings

Run as HTTP MCP on 127.0.0.1:3041/mcp by default. Override port via
FINNHUB_MCP_PORT env var.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server.fastmcp import FastMCP

LOG_DIR = (
    Path(os.environ.get("FINNHUB_MCP_DIR") or (Path.home() / "finnhub-mcp")) / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "finnhub_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("finnhub")

BASE = "https://finnhub.io/api/v1"
HTTP_TIMEOUT_S = 15
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
UA = "alphalens-finnhub-mcp/1.0"

mcp = FastMCP(
    "finnhub",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("FINNHUB_MCP_PORT", "3041")),
)

# ── caches + client-side rate floor ─────────────────────────────────────────
_CACHE: dict[str, tuple[float, Any]] = {}
TTL_NEWS = 10 * 60          # 10min
TTL_RECS = 6 * 60 * 60      # 6h (updated monthly)
TTL_EARN = 60 * 60          # 1h
_MIN_GAP_S = 1.05           # ~1 rps; free tier is 60/min
_rate_lock = asyncio.Lock()
_last_call = {"t": 0.0}


def _cache_get(key: str, ttl: float) -> Any | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > ttl:
        return None
    return val


def _cache_put(key: str, val: Any) -> None:
    _CACHE[key] = (time.time(), val)


async def _fetch(path: str, params: dict) -> Any:
    """GET a Finnhub endpoint with the token, ~1 rps single-concurrency pacing."""
    if not FINNHUB_KEY:
        raise RuntimeError("FINNHUB_API_KEY env var is not set")
    full = dict(params)
    full["token"] = FINNHUB_KEY
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with _rate_lock:  # serialize + enforce min-gap (single concurrency)
        gap = time.time() - _last_call["t"]
        if gap < _MIN_GAP_S:
            await asyncio.sleep(_MIN_GAP_S - gap)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{BASE}/{path.lstrip('/')}", params=full, headers={"User-Agent": UA}) as r:
                text = await r.text()
                _last_call["t"] = time.time()
                if r.status == 429:
                    raise RuntimeError("Finnhub rate limit (429)")
                if r.status != 200:
                    raise RuntimeError(f"Finnhub {path} -> {r.status} {text[:160]}")
                return json.loads(text)


def _iso_from_epoch(sec: Any) -> str | None:
    try:
        return datetime.datetime.fromtimestamp(int(sec), tz=datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


# ── company_news ──────────────────────────────────────────────────────────────
async def _company_news(ticker: str, from_date: str, to_date: str) -> dict:
    ticker = ticker.strip().upper()
    today = datetime.date.today()
    if not (to_date and to_date.strip()):
        to_date = today.isoformat()
    if not (from_date and from_date.strip()):
        from_date = (today - datetime.timedelta(days=1)).isoformat()  # last 24h
    cache_key = f"news:{ticker}:{from_date}:{to_date}"
    cached = _cache_get(cache_key, TTL_NEWS)
    if cached is not None:
        return cached
    data = await _fetch("company-news", {"symbol": ticker, "from": from_date, "to": to_date})
    items = []
    for n in data if isinstance(data, list) else []:
        items.append(
            {
                "datetime": _iso_from_epoch(n.get("datetime")),
                "headline": n.get("headline"),
                "source": n.get("source"),
                "summary": n.get("summary"),
                "url": n.get("url"),
                "id": n.get("id"),
            }
        )
    result = {"success": True, "ticker": ticker, "items": items}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def company_news(ticker: str, from_date: str = "", to_date: str = "") -> str:
    """Finnhub company news for a ticker. from_date/to_date are ISO 'YYYY-MM-DD'
    (default last 24h). Each item: {datetime (ISO8601), headline, source,
    summary, url, id}. Free tier limited to recent (~1yr) news.
    """
    try:
        return json.dumps(await _company_news(ticker, from_date, to_date))
    except Exception as e:
        log.warning("company_news %s failed: %s", ticker, e)
        return json.dumps({"success": False, "ticker": ticker, "error": str(e)})


# ── recommendation_trends ─────────────────────────────────────────────────────
async def _recommendation_trends(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    cache_key = f"rec:{ticker}"
    cached = _cache_get(cache_key, TTL_RECS)
    if cached is not None:
        return cached
    data = await _fetch("stock/recommendation", {"symbol": ticker})
    trends = []
    for t in data if isinstance(data, list) else []:
        trends.append(
            {
                "period": t.get("period"),
                "strong_buy": t.get("strongBuy"),
                "buy": t.get("buy"),
                "hold": t.get("hold"),
                "sell": t.get("sell"),
                "strong_sell": t.get("strongSell"),
            }
        )
    # Finnhub returns newest first; keep that order.
    result = {"success": True, "ticker": ticker, "trends": trends}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def recommendation_trends(ticker: str) -> str:
    """Finnhub analyst recommendation trend for a ticker. Returns monthly
    snapshots, newest first: {period (YYYY-MM-DD), strong_buy, buy, hold, sell,
    strong_sell} (analyst counts in each bucket).
    """
    try:
        return json.dumps(await _recommendation_trends(ticker))
    except Exception as e:
        log.warning("recommendation_trends %s failed: %s", ticker, e)
        return json.dumps({"success": False, "ticker": ticker, "error": str(e)})


# ── earnings_calendar ─────────────────────────────────────────────────────────
async def _earnings_calendar(from_date: str, to_date: str) -> dict:
    today = datetime.date.today()
    if not (from_date and from_date.strip()):
        from_date = today.isoformat()
    if not (to_date and to_date.strip()):
        to_date = (today + datetime.timedelta(days=7)).isoformat()
    cache_key = f"earn:{from_date}:{to_date}"
    cached = _cache_get(cache_key, TTL_EARN)
    if cached is not None:
        return cached
    data = await _fetch("calendar/earnings", {"from": from_date, "to": to_date})
    cal = (data or {}).get("earningsCalendar") or []
    items = []
    for e in cal:
        items.append(
            {
                "date": e.get("date"),
                "symbol": e.get("symbol"),
                "eps_estimate": e.get("epsEstimate"),
                "eps_actual": e.get("epsActual"),
                "revenue_estimate": e.get("revenueEstimate"),
                "revenue_actual": e.get("revenueActual"),
                "hour": e.get("hour"),
            }
        )
    result = {"success": True, "items": items}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def earnings_calendar(from_date: str, to_date: str) -> str:
    """Finnhub earnings calendar between two ISO dates (default today..+7d if
    blank). Each item: {date, symbol, eps_estimate, eps_actual, revenue_estimate,
    revenue_actual, hour (bmo/amc/dmh)}.
    """
    try:
        return json.dumps(await _earnings_calendar(from_date, to_date))
    except Exception as e:
        log.warning("earnings_calendar failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── --probe CLI ────────────────────────────────────────────────────────────
async def _probe(args: list[str]) -> None:
    ticker = args[0] if args else "AAPL"
    out: dict[str, Any] = {"key_set": bool(FINNHUB_KEY)}
    for name, coro in (
        (f"company_news({ticker})", _company_news(ticker, "", "")),
        (f"recommendation_trends({ticker})", _recommendation_trends(ticker)),
        ("earnings_calendar", _earnings_calendar("", "")),
    ):
        try:
            out[name] = await coro
        except Exception as e:
            out[name] = {"success": False, "error": str(e)}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--probe"]
        asyncio.run(_probe(rest))
    else:
        log.info(
            "finnhub MCP starting on http://%s:%s/mcp (key=%s)",
            mcp.settings.host,
            mcp.settings.port,
            "set" if FINNHUB_KEY else "MISSING",
        )
        from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
        serve(mcp)
