"""polymarket_mcp.py -- HTTP MCP server exposing Polymarket data via Gamma API.

Polymarket is a prediction-market platform on Polygon. Each market is a
tradeable contract whose price = market-implied probability of an outcome.
Reading these prices gives a clean, money-weighted signal of what the
crowd actually believes about Fed decisions, elections, CPI, geopolitical
events, sports, crypto milestones, etc.

The Gamma API is free, no auth needed. Just requires a Browser-ish
User-Agent header (without it the server 403s). Trading would need the
CLOB API + a private key but we don't expose any write tools here --
strictly read-only intel.

Tools:
  list_markets(query, limit, active_only, sort_by)
  get_market(id_or_slug)
  list_events(query, limit, active_only)
  get_event(id_or_slug)
  top_movers(window='1mo', limit)

Use cases for market-brief:
  - Macro catalyst calendar w/ implied probabilities
  - Cross-check group/KOL calls against market-implied odds
  - Surface big probability shifts in last 30d

Run as HTTP MCP on 127.0.0.1:3033/mcp by default. Override port via
POLYMARKET_MCP_PORT env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
from mcp.server.fastmcp import FastMCP

LOG_DIR = (
    Path(os.environ.get("POLYMARKET_MCP_DIR") or (Path.home() / "polymarket-mcp"))
    / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "polymarket_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("polymarket")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BASE = "https://gamma-api.polymarket.com"
HTTP_TIMEOUT_S = 10

mcp = FastMCP(
    "polymarket",
    host="127.0.0.1",
    port=int(os.environ.get("POLYMARKET_MCP_PORT", "3033")),
)


async def _fetch(path: str, params: dict | None = None) -> Any:
    """GET a Gamma endpoint, returning parsed JSON. Raises on non-200."""
    url = f"{BASE}{path}"
    if params:
        cleaned = {k: v for k, v in params.items() if v is not None}
        if cleaned:
            url = f"{url}?{urllib.parse.urlencode(cleaned)}"
    headers = {"User-Agent": UA, "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
        async with s.get(url) as r:
            r.raise_for_status()
            return await r.json()


def _parse_json_field(v: Any) -> Any:
    """Gamma serializes outcomes / outcomePrices as JSON strings. Parse them."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _slim_market(m: dict) -> dict:
    """Reduce a market dict (87 keys) to the brief-relevant subset, and
    parse outcomes / outcomePrices into actual lists / floats."""
    outcomes = _parse_json_field(m.get("outcomes")) or []
    prices = _parse_json_field(m.get("outcomePrices")) or []
    try:
        prices = [float(p) for p in prices]
    except Exception:
        pass
    return {
        "id": m.get("id"),
        "slug": m.get("slug"),
        "question": m.get("question"),
        "outcomes": outcomes,
        "outcomePrices": prices,
        "volume24hr": m.get("volume24hr"),
        "volume1wk": m.get("volume1wk"),
        "volume1mo": m.get("volume1mo"),
        "liquidity": m.get("liquidity"),
        "lastTradePrice": m.get("lastTradePrice"),
        "oneMonthPriceChange": m.get("oneMonthPriceChange"),
        "endDate": m.get("endDate"),
        "closed": m.get("closed"),
        "url": (
            f"https://polymarket.com/event/{m['slug']}" if m.get("slug") else None
        ),
    }


def _abs_change(m: dict) -> float:
    v = m.get("oneMonthPriceChange")
    try:
        return abs(float(v)) if v is not None else 0.0
    except Exception:
        return 0.0


@mcp.tool()
async def list_markets(
    query: str = "",
    limit: int = 20,
    active_only: bool = True,
    sort_by: str = "volume24hr",
) -> dict:
    """List Polymarket markets, slimmed to brief-relevant fields.

    Each entry includes outcomePrices = implied probability per outcome
    (0.0 = market says 0%, 1.0 = market says 100%).

    query: case-insensitive substring filter on `question` text. Polymarket's
           Gamma doesn't expose server-side text search, so we fetch a wider
           page (limit*3 up to 200) and filter client-side.
    limit: 1-100.
    active_only: filter closed=false (default True).
    sort_by: volume24hr | volume1wk | volume1mo | liquidity | endDate
             (endDate sorts ascending so the soonest-resolving markets come
             first; everything else sorts descending so biggest comes first).
    """
    limit = max(1, min(100, limit))
    fetch_limit = min(limit * 3, 200) if query else limit
    valid_sorts = {"volume24hr", "volume1wk", "volume1mo", "liquidity", "endDate"}
    if sort_by not in valid_sorts:
        return {"success": False, "error": f"sort_by must be one of {sorted(valid_sorts)}"}
    params = {
        "limit": fetch_limit,
        "order": sort_by,
        "ascending": "true" if sort_by == "endDate" else "false",
        "closed": "false" if active_only else None,
    }
    try:
        data = await _fetch("/markets", params)
    except Exception as e:
        log.exception("list_markets failed")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, list):
        return {"success": False, "error": f"unexpected response shape: {type(data).__name__}"}
    if query:
        ql = query.lower()
        data = [m for m in data if ql in (m.get("question") or "").lower()]
    out = [_slim_market(m) for m in data[:limit]]
    log.info("list_markets q=%r sort=%s -> %d markets", query, sort_by, len(out))
    return {"success": True, "count": len(out), "markets": out}


@mcp.tool()
async def get_market(id_or_slug: str) -> dict:
    """Get full details of one market by numeric id or slug.

    Pass slug for readability (e.g. 'will-bitcoin-hit-150k-by-june-30-2026')
    or the numeric id from a previous list_markets call.
    """
    try:
        if not str(id_or_slug).isdigit():
            data = await _fetch("/markets", {"slug": id_or_slug, "limit": 1})
            if isinstance(data, list) and data:
                return {"success": True, "market": _slim_market(data[0])}
            return {"success": False, "error": f"slug not found: {id_or_slug}"}
        data = await _fetch(f"/markets/{id_or_slug}")
        if isinstance(data, dict):
            return {"success": True, "market": _slim_market(data)}
        return {"success": False, "error": f"id not found: {id_or_slug}"}
    except Exception as e:
        log.exception("get_market %r failed", id_or_slug)
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


def _slim_event(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "slug": e.get("slug"),
        "title": e.get("title"),
        "endDate": e.get("endDate"),
        "volume24hr": e.get("volume24hr"),
        "marketCount": len(e.get("markets") or []),
        "url": (
            f"https://polymarket.com/event/{e['slug']}" if e.get("slug") else None
        ),
    }


@mcp.tool()
async def list_events(
    query: str = "", limit: int = 20, active_only: bool = True
) -> dict:
    """List Polymarket events. An event groups multiple related markets
    (e.g. 'Fed June meeting' has child markets '25bp cut', '50bp cut', 'hold').

    Use this when you want to see all branches of a multi-outcome bet at
    once, vs. list_markets which gives you one yes/no contract per row.
    """
    limit = max(1, min(50, limit))
    fetch_limit = min(limit * 3, 100) if query else limit
    params = {
        "limit": fetch_limit,
        "closed": "false" if active_only else None,
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        data = await _fetch("/events", params)
    except Exception as e:
        log.exception("list_events failed")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, list):
        return {"success": False, "error": f"unexpected response shape: {type(data).__name__}"}
    if query:
        ql = query.lower()
        data = [
            ev for ev in data
            if ql in (ev.get("title") or "").lower()
            or ql in (ev.get("description") or "")[:200].lower()
        ]
    out = [_slim_event(ev) for ev in data[:limit]]
    log.info("list_events q=%r -> %d events", query, len(out))
    return {"success": True, "count": len(out), "events": out}


@mcp.tool()
async def get_event(id_or_slug: str) -> dict:
    """Get an event with all its child markets attached.

    The event payload includes a `markets` array; each child is slimmed
    the same way list_markets returns. Use this to see all outcome
    branches of a multi-leg bet (e.g. Fed meeting cut sizes).
    """
    try:
        if not str(id_or_slug).isdigit():
            data = await _fetch("/events", {"slug": id_or_slug, "limit": 1})
            if isinstance(data, list) and data:
                ev = data[0]
            else:
                return {"success": False, "error": f"slug not found: {id_or_slug}"}
        else:
            ev = await _fetch(f"/events/{id_or_slug}")
        children = [_slim_market(m) for m in (ev.get("markets") or [])]
        return {
            "success": True,
            "id": ev.get("id"),
            "slug": ev.get("slug"),
            "title": ev.get("title"),
            "description": (ev.get("description") or "")[:1000],
            "endDate": ev.get("endDate"),
            "volume24hr": ev.get("volume24hr"),
            "url": (
                f"https://polymarket.com/event/{ev['slug']}"
                if ev.get("slug") else None
            ),
            "markets": children,
        }
    except Exception as e:
        log.exception("get_event %r failed", id_or_slug)
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def top_movers(window: str = "1mo", limit: int = 10) -> dict:
    """Markets ranked by absolute implied-probability change in the window.

    window: only '1mo' is reliably available -- Gamma exposes
            `oneMonthPriceChange` on every market but does NOT expose
            1d / 1w price-change fields. For shorter-timeframe signals
            use list_markets(sort_by='volume24hr'), which surfaces the
            markets that traded the most in the last 24h (= where the
            crowd's attention is concentrated, even if absolute move is
            still small).

    Returns markets with the biggest absolute oneMonthPriceChange,
    descending. Sign of the change is preserved in oneMonthPriceChange
    so the caller can tell direction.
    """
    if window not in ("1mo",):
        return {
            "success": False,
            "error": (
                "window must be '1mo' (Gamma only exposes oneMonthPriceChange). "
                "For shorter windows, use list_markets sort_by='volume24hr'."
            ),
        }
    limit = max(1, min(50, limit))
    try:
        data = await _fetch(
            "/markets",
            {"closed": "false", "limit": 500, "order": "volume1mo", "ascending": "false"},
        )
    except Exception as e:
        log.exception("top_movers failed")
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, list):
        return {"success": False, "error": "unexpected response shape"}
    candidates = [m for m in data if m.get("oneMonthPriceChange") is not None]
    candidates.sort(key=_abs_change, reverse=True)
    out = [_slim_market(m) for m in candidates[:limit]]
    log.info("top_movers window=%s -> %d movers", window, len(out))
    return {"success": True, "window": window, "count": len(out), "markets": out}


if __name__ == "__main__":
    log.info(
        "polymarket MCP starting on http://127.0.0.1:%s/mcp", mcp.settings.port
    )
    mcp.run(transport="streamable-http")
