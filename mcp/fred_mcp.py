"""fred_mcp.py -- HTTP MCP server exposing FRED (St. Louis Fed) economic data.

FRED is the canonical free macro data source: CPI, PPI, NFP, GDP, Fed Funds,
yields, jobless claims, etc. Free API, requires a free API key (register at
https://fred.stlouisfed.org/docs/api/api_key.html). Rate limit 120 req/60s.

Tools:
  get_series(series_id, since='1y', limit=24)   recent observations
  search_series(query, limit=10)                fuzzy find a series
  economic_dashboard()                          latest CPI/PPI/UNRATE/Fed/yields snapshot
  upcoming_releases(days_ahead=14)              high-impact calendar
  series_metadata(series_id)                    title, units, frequency

Use cases for alphalens / market-brief:
  - L2 market regime needs Fed/inflation/jobs context
  - L3 daily brief needs "what economic data drops today"
  - Cross-reference KOL macro calls against the actual data
  - Quick dashboard for /signals page

Run as HTTP MCP on 127.0.0.1:3035/mcp by default. Override port via
FRED_MCP_PORT env var. Set FRED_API_KEY env var (required).
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
from mcp.server.fastmcp import FastMCP

LOG_DIR = (
    Path(os.environ.get("FRED_MCP_DIR") or (Path.home() / "fred-mcp")) / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "fred_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("fred")

BASE = "https://api.stlouisfed.org/fred"
HTTP_TIMEOUT_S = 15
FRED_KEY = os.environ.get("FRED_API_KEY", "")

mcp = FastMCP(
    "fred",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("FRED_MCP_PORT", "3035")),
)

# ── In-memory caches ───────────────────────────────────────────────────────
# observations cached 30 min (monthly data doesn't change often)
# metadata cached 1 day
# release dates cached 1 hour
_CACHE: dict[str, tuple[float, Any]] = {}
TTL_OBSERVATIONS = 30 * 60
TTL_METADATA = 24 * 60 * 60
TTL_RELEASES = 60 * 60


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


async def _fetch(path: str, params: dict | None = None) -> dict:
    """GET an endpoint, returning parsed JSON. Raises on non-200."""
    if not FRED_KEY:
        raise RuntimeError("FRED_API_KEY env var is not set")
    full = dict(params or {})
    full["api_key"] = FRED_KEY
    full["file_type"] = "json"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = f"{BASE}/{path.lstrip('/')}"
        async with session.get(url, params=full) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"FRED {path} -> {r.status} {text[:200]}")
            return json.loads(text)


# ── Date parsing for `since` arg ───────────────────────────────────────────
def _parse_since(s: str) -> str:
    """'1y', '6mo', '90d' → YYYY-MM-DD (today minus that delta)."""
    s = s.strip().lower()
    today = datetime.date.today()
    if s.endswith("y"):
        years = int(s[:-1])
        return today.replace(year=today.year - years).isoformat()
    if s.endswith("mo"):
        months = int(s[:-2])
        # crude: 30d per month
        return (today - datetime.timedelta(days=months * 30)).isoformat()
    if s.endswith("d"):
        days = int(s[:-1])
        return (today - datetime.timedelta(days=days)).isoformat()
    # already a date?
    try:
        datetime.date.fromisoformat(s)
        return s
    except ValueError:
        return (today - datetime.timedelta(days=365)).isoformat()


# ── Tools ──────────────────────────────────────────────────────────────────
@mcp.tool()
async def get_series(series_id: str, since: str = "1y", limit: int = 24) -> dict:
    """Pull recent observations for a FRED series.

    series_id: FRED ID like 'CPIAUCSL' (CPI), 'UNRATE' (unemployment),
               'FEDFUNDS' (Fed funds rate), 'DGS10' (10Y yield).
    since: lookback window '1y', '6mo', '90d' or 'YYYY-MM-DD'.
    limit: max observations to return (most recent first).
    """
    cache_key = f"obs:{series_id}:{since}:{limit}"
    cached = _cache_get(cache_key, TTL_OBSERVATIONS)
    if cached is not None:
        return cached

    start = _parse_since(since)
    # FRED returns ascending; we'll reverse and trim.
    data = await _fetch(
        "series/observations",
        {"series_id": series_id, "observation_start": start, "sort_order": "desc", "limit": limit},
    )
    observations = []
    for o in data.get("observations", []):
        val = o.get("value")
        # FRED uses '.' for missing
        try:
            num = float(val) if val and val != "." else None
        except ValueError:
            num = None
        observations.append({"date": o.get("date"), "value": num})

    # also fetch metadata to enrich
    meta_cache = _cache_get(f"meta:{series_id}", TTL_METADATA)
    if meta_cache is None:
        try:
            mdata = await _fetch("series", {"series_id": series_id})
            meta_cache = mdata.get("seriess", [{}])[0] if mdata.get("seriess") else {}
            _cache_put(f"meta:{series_id}", meta_cache)
        except Exception:
            meta_cache = {}

    latest_change = None
    if len(observations) >= 2 and observations[0]["value"] is not None and observations[1]["value"] is not None:
        latest_change = observations[0]["value"] - observations[1]["value"]

    result = {
        "series_id": series_id,
        "title": meta_cache.get("title"),
        "units": meta_cache.get("units_short") or meta_cache.get("units"),
        "frequency": meta_cache.get("frequency_short") or meta_cache.get("frequency"),
        "seasonal_adjustment": meta_cache.get("seasonal_adjustment_short"),
        "last_updated": meta_cache.get("last_updated"),
        "since": start,
        "count": len(observations),
        "latest": observations[0] if observations else None,
        "latest_change": latest_change,
        "observations": observations,
    }
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def series_metadata(series_id: str) -> dict:
    """Title, units, frequency, seasonal adjustment, last-updated for a series."""
    cached = _cache_get(f"meta:{series_id}", TTL_METADATA)
    if cached is not None:
        return {"series_id": series_id, **cached}
    data = await _fetch("series", {"series_id": series_id})
    s = (data.get("seriess") or [{}])[0]
    _cache_put(f"meta:{series_id}", s)
    return {"series_id": series_id, **s}


@mcp.tool()
async def search_series(query: str, limit: int = 10) -> dict:
    """Fuzzy-search FRED for series matching a query.

    Returns id + title + frequency + last-updated for ranked matches. Use this
    when you don't know the FRED ID for what you're looking for (e.g. 'wage
    growth' → CES0500000003).
    """
    data = await _fetch(
        "series/search",
        {"search_text": query, "limit": limit, "order_by": "popularity"},
    )
    rows = []
    for s in data.get("seriess", []):
        rows.append(
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "frequency": s.get("frequency_short"),
                "units": s.get("units_short"),
                "last_updated": s.get("last_updated"),
                "popularity": s.get("popularity"),
            }
        )
    return {"query": query, "count": len(rows), "results": rows}


# Curated high-impact series for the macro dashboard.
DASHBOARD_SERIES: list[tuple[str, str]] = [
    ("CPIAUCSL", "CPI (Headline, SA)"),
    ("CPILFESL", "Core CPI (SA)"),
    ("PCEPI", "PCE Headline"),
    ("PCEPILFE", "Core PCE"),
    ("PPIFIS", "PPI Final Demand"),
    ("UNRATE", "Unemployment Rate"),
    ("PAYEMS", "Nonfarm Payrolls"),
    ("ICSA", "Initial Jobless Claims"),
    ("FEDFUNDS", "Fed Funds Effective"),
    ("DGS10", "10Y Treasury Yield"),
    ("DGS2", "2Y Treasury Yield"),
    ("T10Y2Y", "10Y-2Y Spread"),
    ("DTWEXBGS", "Trade-Weighted Dollar (Broad)"),
    ("WALCL", "Fed Balance Sheet"),
    ("UMCSENT", "U Michigan Sentiment"),
]


@mcp.tool()
async def economic_dashboard() -> dict:
    """One-shot snapshot of the most-watched US macro series.

    Returns latest value + recent change + units for ~15 series covering
    inflation (CPI/Core CPI/PCE/PPI), labor (UNRATE/NFP/Claims), monetary
    (Fed Funds), rates (10Y/2Y/spread), and sentiment.
    """
    results: dict[str, Any] = {}
    failures: list[str] = []
    sem = asyncio.Semaphore(5)  # FRED rate limit friendly

    async def one(sid: str, label: str) -> None:
        async with sem:
            try:
                d = await get_series(sid, since="6mo", limit=6)
                results[sid] = {
                    "label": label,
                    "title": d.get("title") or label,
                    "units": d.get("units"),
                    "frequency": d.get("frequency"),
                    "latest_date": d.get("latest", {}).get("date") if d.get("latest") else None,
                    "latest_value": d.get("latest", {}).get("value") if d.get("latest") else None,
                    "prev_value": d["observations"][1]["value"] if len(d.get("observations", [])) > 1 else None,
                    "change": d.get("latest_change"),
                }
            except Exception as e:
                failures.append(f"{sid}: {e}")

    await asyncio.gather(*[one(sid, lbl) for sid, lbl in DASHBOARD_SERIES])
    return {
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "series_count": len(results),
        "series": results,
        "failures": failures,
    }


# Curated high-impact releases. release_id from FRED /releases.
HIGH_IMPACT_RELEASES: list[tuple[int, str]] = [
    (10, "CPI"),
    (50, "Employment Situation (NFP)"),
    (51, "PPI"),
    (53, "GDP"),
    (84, "FOMC Statement"),
    (246, "Initial Claims"),
    (116, "Retail Sales"),
    (21, "Industrial Production"),
    (110, "Personal Income & Outlays (PCE)"),
    (192, "U Michigan Sentiment (final)"),
    (175, "ADP Employment"),
    (151, "JOLTS"),
    (16, "ISM Manufacturing"),
    (17, "ISM Services"),
]


@mcp.tool()
async def upcoming_releases(days_ahead: int = 14) -> dict:
    """Calendar of high-impact economic data releases in the next N days.

    Iterates the curated HIGH_IMPACT_RELEASES list and returns each scheduled
    release date that falls in [today, today+days_ahead]. Use for L3 daily
    brief 'what to watch this week' / FOMC week flagging.
    """
    cache_key = f"calendar:{days_ahead}"
    cached = _cache_get(cache_key, TTL_RELEASES)
    if cached is not None:
        return cached

    today = datetime.date.today()
    end = today + datetime.timedelta(days=days_ahead)
    upcoming: list[dict] = []
    sem = asyncio.Semaphore(5)

    async def one(rid: int, name: str) -> None:
        async with sem:
            try:
                # Pull most recent 60 dates (desc); FRED publishes future dates
                # mixed with past — filter to [today, end] window. asc + limit
                # would only return the very oldest dates, all historical.
                d = await _fetch(
                    f"release/dates",
                    {
                        "release_id": rid,
                        "include_release_dates_with_no_data": "true",
                        "sort_order": "desc",
                        "limit": 60,
                    },
                )
                for r in d.get("release_dates", []):
                    try:
                        date = datetime.date.fromisoformat(r["date"])
                    except (KeyError, ValueError):
                        continue
                    if today <= date <= end:
                        upcoming.append({"release_id": rid, "name": name, "date": r["date"]})
            except Exception as e:
                log.warning("release_dates %d failed: %s", rid, e)

    await asyncio.gather(*[one(rid, name) for rid, name in HIGH_IMPACT_RELEASES])
    upcoming.sort(key=lambda r: r["date"])
    result = {
        "today": today.isoformat(),
        "window_days": days_ahead,
        "count": len(upcoming),
        "releases": upcoming,
    }
    _cache_put(cache_key, result)
    return result


if __name__ == "__main__":
    log.info(
        "fred MCP starting on http://127.0.0.1:%s/mcp (key=%s)",
        mcp.settings.port,
        "set" if FRED_KEY else "MISSING",
    )
    from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
    serve(mcp)
