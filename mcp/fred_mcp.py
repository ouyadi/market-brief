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
import html
import json
import logging
import os
import re
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
FED_FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
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


async def _fetch_text(url: str) -> str:
    """GET a non-FRED page, returning text. Used for official calendar pages."""
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"User-Agent": "alphalens-fred-mcp/1.0"}) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"GET {url} -> {r.status} {text[:200]}")
            return text


MONTHS: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    d = datetime.date(year, month, 1)
    while d.weekday() != weekday:
        d += datetime.timedelta(days=1)
    return d + datetime.timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> datetime.date:
    if month == 12:
        d = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    while d.weekday() != weekday:
        d -= datetime.timedelta(days=1)
    return d


def _observed(d: datetime.date) -> datetime.date:
    if d.weekday() == 5:  # Saturday
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:  # Sunday
        return d + datetime.timedelta(days=1)
    return d


def _us_federal_holidays(year: int) -> set[datetime.date]:
    return {
        _observed(datetime.date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),   # MLK
        _nth_weekday(year, 2, 0, 3),   # Presidents' Day
        _last_weekday(year, 5, 0),     # Memorial Day
        _observed(datetime.date(year, 6, 19)),
        _observed(datetime.date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _observed(datetime.date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(datetime.date(year, 12, 25)),
    }


def _is_business_day(d: datetime.date) -> bool:
    return d.weekday() < 5 and d not in _us_federal_holidays(d.year)


def _nth_business_day(year: int, month: int, n: int) -> datetime.date:
    d = datetime.date(year, month, 1)
    seen = 0
    while True:
        if _is_business_day(d):
            seen += 1
            if seen == n:
                return d
        d += datetime.timedelta(days=1)


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


# Curated high-impact releases. release_id from FRED /releases. FRED gives the
# calendar date, but not the intraday release time; time_et is the standard
# scheduled release time used by the publishing agency.
HIGH_IMPACT_RELEASES: list[tuple[int, str, str]] = [
    (10, "CPI", "08:30"),
    (50, "Employment Situation (NFP)", "08:30"),
    (46, "PPI", "08:30"),
    (53, "GDP", "08:30"),
    (101, "FOMC Statement", "14:00"),
    (180, "Initial Claims", "08:30"),
    (9, "Retail Sales", "08:30"),
    (13, "Industrial Production", "09:15"),
    (54, "Personal Income & Outlays (PCE)", "08:30"),
    (91, "U Michigan Sentiment", "10:00"),
    (194, "ADP Employment", "08:15"),
    (192, "JOLTS", "10:00"),
    # ISM Manufacturing / Services do not have FRED release IDs. Do not map
    # them to unrelated releases; use a separate calendar source if needed.
]


def _release(
    *,
    name: str,
    date: datetime.date,
    time_et: str,
    source: str,
    release_id: int | None = None,
    url: str | None = None,
) -> dict:
    return {
        "release_id": release_id,
        "name": name,
        "date": date.isoformat(),
        "time_et": time_et,
        "timezone": "America/New_York",
        "source": source,
        **({"url": url} if url else {}),
    }


async def _fed_fomc_releases(today: datetime.date, end: datetime.date) -> list[dict]:
    """FOMC statement dates from the official Federal Reserve calendar page."""
    try:
        page = await _fetch_text(FED_FOMC_CALENDAR_URL)
    except Exception as e:
        log.warning("fed fomc calendar failed: %s", e)
        return []

    out: list[dict] = []
    year_pat = re.compile(
        r"<h4>\s*<a id=\"\d+\">(?P<year>\d{4})\s+FOMC Meetings</a></h4>.*?"
        r"(?=<h4>\s*<a id=\"\d+\">\d{4}\s+FOMC Meetings</a></h4>|$)",
        re.I | re.S,
    )
    meeting_pat = re.compile(
        r"fomc-meeting__month[^>]*>\s*<strong>(?P<month>[A-Za-z]+)</strong>.*?"
        r"fomc-meeting__date[^>]*>\s*(?P<date>[^<]+)",
        re.I | re.S,
    )
    for ymatch in year_pat.finditer(page):
        year = int(ymatch.group("year"))
        if year < today.year or year > end.year + 1:
            continue
        section = ymatch.group(0)
        for mm in meeting_pat.finditer(section):
            month = MONTHS.get(mmatch_month := mm.group("month").lower())
            if not month:
                log.warning("unknown FOMC month %s", mmatch_month)
                continue
            raw_date = html.unescape(mm.group("date"))
            nums = re.findall(r"\d{1,2}", raw_date)
            if not nums:
                continue
            # A two-day meeting releases the statement on the final listed day.
            day = int(nums[-1])
            try:
                d = datetime.date(year, month, day)
            except ValueError:
                continue
            if today <= d <= end:
                out.append(
                    _release(
                        release_id=101,
                        name="FOMC Statement",
                        date=d,
                        time_et="14:00",
                        source="fed:fomc_calendar",
                        url=FED_FOMC_CALENDAR_URL,
                    )
                )
    return out


def _ism_releases(today: datetime.date, end: datetime.date) -> list[dict]:
    """ISM PMI report dates from ISM's stated monthly release cadence.

    Manufacturing PMI is released on the first business day at 10:00 ET,
    except January, when it is released on the second business day. Services
    PMI is released on the third business day at 10:00 ET.
    """
    out: list[dict] = []
    cursor = datetime.date(today.year, today.month, 1)
    last = datetime.date(end.year, end.month, 1)
    while cursor <= last:
        year, month = cursor.year, cursor.month
        mfg_day = _nth_business_day(year, month, 2 if month == 1 else 1)
        svc_day = _nth_business_day(year, month, 3)
        if today <= mfg_day <= end:
            out.append(
                _release(
                    name="ISM Manufacturing PMI",
                    date=mfg_day,
                    time_et="10:00",
                    source="ism:release_cadence",
                    url="https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/",
                )
            )
        if today <= svc_day <= end:
            out.append(
                _release(
                    name="ISM Services PMI",
                    date=svc_day,
                    time_et="10:00",
                    source="ism:release_cadence",
                    url="https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/",
                )
            )
        if month == 12:
            cursor = datetime.date(year + 1, 1, 1)
        else:
            cursor = datetime.date(year, month + 1, 1)
    return out


@mcp.tool()
async def upcoming_releases(days_ahead: int = 14) -> dict:
    """Calendar of high-impact economic data releases in the next N days.

    Iterates the curated HIGH_IMPACT_RELEASES list and returns each scheduled
    release date/time that falls in [today, today+days_ahead]. Use for L3 daily
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

    async def one(rid: int, name: str, time_et: str) -> None:
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
                        upcoming.append(
                            _release(
                                release_id=rid,
                                name=name,
                                date=date,
                                time_et=time_et,
                                source="fred:release_dates",
                            )
                        )
            except Exception as e:
                log.warning("release_dates %d failed: %s", rid, e)

    await asyncio.gather(*[one(rid, name, time_et) for rid, name, time_et in HIGH_IMPACT_RELEASES])
    upcoming.extend(await _fed_fomc_releases(today, end))
    upcoming.extend(_ism_releases(today, end))
    # Dedupe by event family/date/time/source class. FRED and Fed can both
    # surface FOMC in some years; prefer the official Fed calendar row.
    deduped: dict[tuple[str, str, str], dict] = {}
    for r in upcoming:
        key = (r["name"], r["date"], r["time_et"])
        prev = deduped.get(key)
        if not prev or str(r.get("source", "")).startswith("fed:"):
            deduped[key] = r
    upcoming = sorted(deduped.values(), key=lambda r: (r["date"], r["time_et"], r["name"]))
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
