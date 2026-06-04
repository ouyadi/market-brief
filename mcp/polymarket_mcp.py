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
import datetime
import json
import logging
import math
import os
import re
import statistics
import time
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
# CLOB exposes /prices-history per-token-id. Gamma doesn't have history;
# this is where 1h/1d/1w/etc. probability changes come from.
CLOB_BASE = "https://clob.polymarket.com"
HTTP_TIMEOUT_S = 15

mcp = FastMCP(
    "polymarket",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("POLYMARKET_MCP_PORT", "3033")),
)


async def _fetch(path: str, params: dict | None = None, base: str = BASE) -> Any:
    """GET an endpoint at the given base, returning parsed JSON. Raises on non-200."""
    url = f"{base}{path}"
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


# ───────────────────────── Time-series helpers (CLOB) ──────────────────────


async def _resolve_market(id_or_slug: str) -> dict | None:
    """Fetch the full Gamma market dict by id or slug. None if not found.
    Centralizes the "look up by slug if not digits, otherwise by id" branch
    so each tool doesn't repeat the logic.
    """
    try:
        if not str(id_or_slug).isdigit():
            data = await _fetch("/markets", {"slug": id_or_slug, "limit": 1})
            if isinstance(data, list) and data:
                return data[0]
            return None
        data = await _fetch(f"/markets/{id_or_slug}")
        return data if isinstance(data, dict) else None
    except Exception:
        log.exception("_resolve_market %r failed", id_or_slug)
        return None


def _yes_token_id(m: dict) -> str | None:
    """Extract the Yes-outcome CLOB token_id from a market dict. The
    clobTokenIds field is a JSON-encoded string in Gamma's response."""
    raw = m.get("clobTokenIds")
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if isinstance(raw, list) and raw:
        return str(raw[0])
    return None


async def _price_history(
    token_id: str, lookback_seconds: int, fidelity_minutes: int
) -> list[dict]:
    """Fetch CLOB /prices-history. Returns list of {t: epoch_sec, p: float}.
    Empty list on any issue (logged) so callers can degrade gracefully.

    CLOB has two query modes, with non-obvious constraints:
      * startTs/endTs+fidelity: only accepted for windows up to ~7 days,
        returns HTTP 400 otherwise.
      * interval=NAME+fidelity: named intervals are '1h', '6h', '1d',
        '1w', '1m'. '1m' here means 1 MONTH, not 1 minute. Beyond '1m'
        (~30 days) no longer/shorter window is exposed by the API.

    We pick the right mode automatically:
      lookback <= 7d  -> startTs/endTs
      lookback >  7d  -> interval='1m', client-side filter to the
                         requested cutoff. If the caller asks for >30d
                         we return what's available and add a note.
    """
    end = int(time.time())
    seven_days = 7 * 86400
    if lookback_seconds <= seven_days:
        params = {
            "market": token_id,
            "startTs": end - lookback_seconds,
            "endTs": end,
            "fidelity": fidelity_minutes,
        }
    else:
        # interval=1m covers ~30 days at the chosen fidelity. The CLOB
        # endpoint won't give us more than that even with explicit time
        # ranges, so anything beyond is best-effort + filter.
        params = {
            "market": token_id,
            "interval": "1m",
            "fidelity": fidelity_minutes,
        }
    try:
        data = await _fetch("/prices-history", params, base=CLOB_BASE)
    except Exception as e:
        log.warning(
            "price_history %s lookback=%ds failed: %s",
            token_id[:12], lookback_seconds, e,
        )
        return []
    hist = data.get("history") or []
    # If we used a named interval, prune points older than the requested
    # cutoff so callers see exactly the window they asked for.
    if "interval" in params:
        cutoff = end - lookback_seconds
        hist = [pt for pt in hist if int(pt.get("t", 0)) >= cutoff]
    return hist


_LOOKBACK_RE = re.compile(r"^\s*(\d+)\s*(mo|h|d|w|m)\s*$", re.I)


def _parse_lookback(s: str) -> int | None:
    """'24h' / '7d' / '1w' / '1mo' / '30m' -> seconds. None if unparseable.
    Matches '1mo' BEFORE 'm' because regex tries alternations left-to-right
    in the unit group."""
    match = _LOOKBACK_RE.match(s or "")
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2).lower()
    table = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000}
    return n * table[unit]


# ─────────────────────────────── New tools ─────────────────────────────────


@mcp.tool()
async def get_price_history(
    id_or_slug: str,
    lookback_hours: int = 168,
    fidelity_minutes: int = 60,
) -> dict:
    """Time series of implied probability for one market via CLOB.

    Use this to plot or compute custom statistics from the probability path.
    The Yes outcome's price = implied prob the event resolves Yes (range 0-1).

    lookback_hours: 1-8760 (1 year max). Default 168 = 7 days.
    fidelity_minutes: 1-1440. Smaller = more points but larger response.
                      Default 60 (1 sample/hour).

    Returns: points sorted oldest first, each {ts: ISO8601, p: float}.
    """
    lookback_hours = max(1, min(8760, lookback_hours))
    fidelity_minutes = max(1, min(1440, fidelity_minutes))

    m = await _resolve_market(id_or_slug)
    if not m:
        return {"success": False, "error": f"market not found: {id_or_slug}"}
    tok = _yes_token_id(m)
    if not tok:
        return {"success": False, "error": "market has no clobTokenIds"}

    hist = await _price_history(tok, lookback_hours * 3600, fidelity_minutes)
    if not hist:
        return {"success": False, "error": "no history returned from CLOB"}

    points = []
    for pt in hist:
        try:
            ts = int(pt["t"])
            points.append(
                {
                    "ts": datetime.datetime.utcfromtimestamp(ts).isoformat() + "Z",
                    "p": float(pt["p"]),
                }
            )
        except Exception:
            continue
    current = None
    prices = _parse_json_field(m.get("outcomePrices"))
    if isinstance(prices, list) and prices:
        try:
            current = float(prices[0])
        except Exception:
            pass
    log.info("get_price_history %s -> %d points", m.get("slug"), len(points))
    return {
        "success": True,
        "slug": m.get("slug"),
        "question": m.get("question"),
        "current": current,
        "lookback_hours": lookback_hours,
        "fidelity_minutes": fidelity_minutes,
        "count": len(points),
        "points": points,
    }


@mcp.tool()
async def prob_change(id_or_slug: str, lookback: str = "24h") -> dict:
    """Probability change of a market over a string-spec lookback window.

    Fills the Gamma-API gap: Gamma exposes oneMonthPriceChange only.
    This works for any window from minutes to weeks.

    lookback: '15m' / '1h' / '6h' / '24h' / '3d' / '7d' / '1w' / '1mo' etc.
              Suffix m=minute h=hour d=day w=week mo=month.

    Returns: then / now / delta_abs / delta_pct_relative. delta_pct_relative
    is None when the prior price is exactly 0 (avoids div-by-zero).
    """
    secs = _parse_lookback(lookback)
    if not secs:
        return {"success": False, "error": f"invalid lookback: {lookback!r} (use e.g. '24h', '7d')"}

    m = await _resolve_market(id_or_slug)
    if not m:
        return {"success": False, "error": f"market not found: {id_or_slug}"}
    tok = _yes_token_id(m)
    if not tok:
        return {"success": False, "error": "market has no clobTokenIds"}

    # Pick fidelity to get >=10 points across the window.
    # Cap at 720 min (12h) -- CLOB rejects very-coarse sampling silently
    # (returns empty history). Floor at 1 so we never request 0.
    fidelity = max(1, min(720, secs // (10 * 60)))
    hist = await _price_history(tok, secs, fidelity)
    if len(hist) < 2:
        return {"success": False, "error": f"insufficient history points (got {len(hist)})"}

    try:
        p0 = float(hist[0]["p"])
        p1 = float(hist[-1]["p"])
    except Exception as e:
        return {"success": False, "error": f"bad history shape: {e}"}

    return {
        "success": True,
        "slug": m.get("slug"),
        "question": m.get("question"),
        "lookback": lookback,
        "then": p0,
        "now": p1,
        "delta_abs": p1 - p0,
        "delta_pct_relative": (p1 - p0) / p0 if p0 else None,
        "points_used": len(hist),
    }


@mcp.tool()
async def compute_vol(
    id_or_slug: str,
    lookback_days: int = 7,
    annualize_to_days: int = 365,
) -> dict:
    """Realized volatility of implied-probability log-returns, annualized.

    Treats P(t) as a price-like series, computes log(P(t+1)/P(t)) across
    1-hour samples, takes stdev, annualizes by sqrt(24 * annualize_to_days).

    Note: this is NOT options IV in the Black-Scholes sense -- Polymarket
    contracts aren't options. Interpret as an "agreement vs disagreement"
    metric: high vol = market still actively debating; low vol = consensus
    (or thin liquidity, which looks the same on a chart).

    lookback_days: 1-90.
    annualize_to_days: usually 365. Use 252 for trading-days analog.
    """
    lookback_days = max(1, min(90, lookback_days))
    secs = lookback_days * 86400

    m = await _resolve_market(id_or_slug)
    if not m:
        return {"success": False, "error": f"market not found: {id_or_slug}"}
    tok = _yes_token_id(m)
    if not tok:
        return {"success": False, "error": "market has no clobTokenIds"}

    hist = await _price_history(tok, secs, 60)  # 1-hour sampling
    if len(hist) < 10:
        return {"success": False, "error": f"insufficient history ({len(hist)} points)"}

    # Filter to interior prices (skip 0/1 corners where log-return blows up)
    prices = [float(pt["p"]) for pt in hist if 0 < float(pt.get("p", 0)) < 1]
    if len(prices) < 10:
        return {
            "success": False,
            "error": (
                "too many corner prices (0 or 1) to compute log-returns -- "
                "market is consensus-resolved-in-direction, vol is effectively 0"
            ),
        }

    log_rets: list[float] = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0:
            log_rets.append(math.log(prices[i] / prices[i - 1]))
    if len(log_rets) < 5:
        return {"success": False, "error": "insufficient log-returns"}

    sd = statistics.stdev(log_rets)
    periods_per_year = 24 * annualize_to_days  # 24 hourly samples * days
    annualized = sd * math.sqrt(periods_per_year)

    log.info(
        "compute_vol %s lookback=%dd -> vol=%.3f (n=%d)",
        m.get("slug"), lookback_days, annualized, len(log_rets),
    )
    return {
        "success": True,
        "slug": m.get("slug"),
        "question": m.get("question"),
        "lookback_days": lookback_days,
        "annualize_to_days": annualize_to_days,
        "sample_count": len(log_rets),
        "stdev_log_return": sd,
        "annualized_vol": annualized,
        "current_price": prices[-1] if prices else None,
    }


@mcp.tool()
async def short_movers(
    window_hours: int = 24, limit: int = 10, scan_size: int = 50
) -> dict:
    """Top markets ranked by absolute probability change over the past N hours.

    Gamma only exposes oneMonthPriceChange; this fills the gap for 1h/1d/1w.
    Implementation: take top `scan_size` markets by 24h volume from Gamma,
    fetch each's CLOB price history over the window in parallel, sort by
    |delta_abs|.

    window_hours: 1-168 (up to 1 week).
    limit: 1-20 results returned.
    scan_size: 10-100 markets considered. More = broader coverage, slower.

    Cost: ~scan_size CLOB requests in parallel. Typical 50-market scan
    completes in 5-15s. Don't poll this frequently.
    """
    window_hours = max(1, min(168, window_hours))
    limit = max(1, min(20, limit))
    scan_size = max(10, min(100, scan_size))
    secs = window_hours * 3600
    # Same fidelity cap as prob_change -- 720 min ceiling avoids CLOB
    # silently returning empty history at too-coarse sampling.
    fidelity = max(5, min(720, secs // (10 * 60)))

    try:
        markets = await _fetch(
            "/markets",
            {
                "closed": "false",
                "limit": scan_size,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(markets, list) or not markets:
        return {"success": False, "error": "no markets returned"}

    async def _one(m: dict) -> dict | None:
        tok = _yes_token_id(m)
        if not tok:
            return None
        hist = await _price_history(tok, secs, fidelity)
        if len(hist) < 2:
            return None
        try:
            p0 = float(hist[0]["p"])
            p1 = float(hist[-1]["p"])
        except Exception:
            return None
        return {
            "slug": m.get("slug"),
            "question": m.get("question"),
            "then": p0,
            "now": p1,
            "delta_abs": p1 - p0,
            "delta_pct_relative": (p1 - p0) / p0 if p0 else None,
            "current_outcomePrices": _parse_json_field(m.get("outcomePrices")),
            "endDate": m.get("endDate"),
            "url": (
                f"https://polymarket.com/event/{m['slug']}" if m.get("slug") else None
            ),
        }

    results = await asyncio.gather(*[_one(m) for m in markets])
    movers = [r for r in results if r is not None]
    movers.sort(key=lambda x: abs(x["delta_abs"]), reverse=True)
    log.info(
        "short_movers window=%dh scan=%d -> %d valid, top |Δ|=%.4f",
        window_hours, len(markets), len(movers),
        abs(movers[0]["delta_abs"]) if movers else 0,
    )
    return {
        "success": True,
        "window_hours": window_hours,
        "scan_size": scan_size,
        "scanned": len(markets),
        "matched": len(movers),
        "movers": movers[:limit],
    }


if __name__ == "__main__":
    log.info(
        "polymarket MCP starting on http://127.0.0.1:%s/mcp", mcp.settings.port
    )
    from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
    serve(mcp)
