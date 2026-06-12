"""shorts_mcp.py -- HTTP MCP server exposing short-sale data (FINRA + iBorrowDesk).

Two free short-side sources:

  FINRA Query API (api.finra.org/data/group/otcMarket):
    - regShoDaily          per-day consolidated short *volume* (covers listed
                           names via the Trade Reporting Facilities, multiple
                           rows per symbol/day -> aggregated here).
    - equityShortInterest  bi-monthly short *interest* (FINRA Rule 4560 -- this
                           is OTC equities ONLY; listed names like NVDA/AAPL do
                           NOT appear, so short_interest()/short_interest_latest()
                           will be empty for most watchlist tickers. Use
                           short_volume() for listed-name short pressure.).
    Auth: FINRA's anonymous tier serves both endpoints at a low rate limit
    (verified 2026-06-12). OAuth2 client_credentials raises the limit; we send
    a bearer token when FINRA_API_CLIENT_ID / FINRA_API_CLIENT_SECRET are in the
    env, and fall back to anonymous when they aren't. Credentials are read ONLY
    from the environment -- never hardcoded.

  iBorrowDesk borrow (www.iborrowdesk.com/api/ticker/{T}, browser UA required):
    a per-ticker JSON mirror of IBKR's shortstock feed. Returns latest_fee
    (annualized %, same unit as IBKR usa.txt -> no conversion downstream),
    latest_available, and a daily[] series carrying the rebate rate. There is
    NO bulk file -- it's per-ticker, so tickers are REQUIRED (no whole-market
    pull). The retired IBKR FTP path (ftp3.interactivebrokers.com login
    "shortstock") was server-side blocked/down as of 2026-06-12. Each ticker
    is cached 4h; client-side pacing 1.8s/call with a 3-fail circuit breaker.

short_volume != short_interest: volume is shares shorted *that day* (a flow);
interest is total open short shares at the settlement snapshot (a stock).

Tools (per-ticker + bulk):
  short_interest(ticker, periods=6)          SI snapshots (OTC-only, see above)
  short_volume(ticker, days=30)              daily short volume + ratio
  borrow(ticker)                             iBorrowDesk fee/rebate/available
  short_volume_day(date="", tickers=[])      one day, optional ticker filter
  short_interest_latest(tickers=[])          latest SI snapshot, ticker filter
  borrow_snapshot(tickers=[])                iBorrowDesk fee/available (tickers REQUIRED)

Run as HTTP MCP on 127.0.0.1:3040/mcp by default. Override port via
SHORTS_MCP_PORT env var.
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
    Path(os.environ.get("SHORTS_MCP_DIR") or (Path.home() / "shorts-mcp")) / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "shorts_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("shorts")

HTTP_TIMEOUT_S = 15
UA = "alphalens-shorts-mcp/1.0"

FINRA_DATA_BASE = "https://api.finra.org/data/group/otcMarket/name"
# OAuth2 client_credentials token endpoint (FINRA FIP). Token cached to expiry.
FINRA_TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
FINRA_CLIENT_ID = os.environ.get("FINRA_API_CLIENT_ID", "")
FINRA_CLIENT_SECRET = os.environ.get("FINRA_API_CLIENT_SECRET", "")

# Dataset names verified against the public FINRA metadata endpoint 2026-06-12:
#   /metadata/group/otcMarket/name/regShoDaily      -> datasetName REGSHODAILY
#   /metadata/group/otcMarket/name/equityShortInterest -> EQUITYSHORTINTEREST
DS_REG_SHO = "regShoDaily"
DS_SHORT_INTEREST = "equityShortInterest"

# iBorrowDesk: per-ticker JSON mirror of IBKR shortstock. 403s on a non-browser
# UA (UA filter, not IP filter -- so OCI/datacenter should pass). aiohttp follows
# the www redirect by default; we hit www directly to save a hop.
IBD_URL = "https://www.iborrowdesk.com/api/ticker/{ticker}"
IBD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_IBD_GAP_S = 1.8             # client-side pacing between iBorrowDesk calls
_IBD_MAX_TICKERS = 80        # per-ticker API; cap a snapshot request
_IBD_FAIL_STREAK_LIMIT = 3   # consecutive hard failures -> open the circuit

mcp = FastMCP(
    "shorts",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("SHORTS_MCP_PORT", "3040")),
)

# ── caches ────────────────────────────────────────────────────────────────
_CACHE: dict[str, tuple[float, Any]] = {}
TTL_FINRA = 60 * 60          # 1h (FINRA data updates daily/bi-monthly)
TTL_BORROW = 4 * 60 * 60     # 4h (iBorrowDesk refreshes intraday but slow)
_TOKEN: dict[str, Any] = {"value": None, "exp": 0.0}
# iBorrowDesk pacing -- its own lock/last-call, NOT shared with FINRA.
_ibd_lock = asyncio.Lock()
_ibd_last_call = {"t": 0.0}


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


def _to_int(v: Any) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── FINRA OAuth2 + query ────────────────────────────────────────────────────
async def _finra_token() -> str | None:
    """client_credentials token, cached to expiry. None when no creds set
    (the API still serves anonymously at a lower rate limit)."""
    if not (FINRA_CLIENT_ID and FINRA_CLIENT_SECRET):
        return None
    if _TOKEN["value"] and time.time() < _TOKEN["exp"] - 60:
        return _TOKEN["value"]
    import base64

    basic = base64.b64encode(f"{FINRA_CLIENT_ID}:{FINRA_CLIENT_SECRET}".encode()).decode()
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(
            FINRA_TOKEN_URL,
            params={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}", "User-Agent": UA},
        ) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"FINRA token -> {r.status} {text[:160]}")
            tok = json.loads(text)
    _TOKEN["value"] = tok.get("access_token")
    _TOKEN["exp"] = time.time() + float(tok.get("expires_in", 1800))
    return _TOKEN["value"]


async def _finra_query(dataset: str, body: dict) -> list[dict]:
    """POST a FINRA data query. Returns [] on 204/empty. Bearer token attached
    when creds are configured, else anonymous."""
    token = None
    try:
        token = await _finra_token()
    except Exception as e:  # token failure shouldn't kill anonymous access
        log.warning("FINRA token failed, falling back to anonymous: %s", e)
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(f"{FINRA_DATA_BASE}/{dataset}", json=body, headers=headers) as r:
            text = await r.text()
            if r.status in (204,) or not text.strip():
                return []
            if r.status != 200:
                raise RuntimeError(f"FINRA {dataset} -> {r.status} {text[:160]}")
            data = json.loads(text)
            return data if isinstance(data, list) else []


def _eq(field: str, value: str) -> dict:
    return {"compareType": "equal", "fieldName": field, "fieldValue": value}


def _date_range(field: str, start: str, end: str) -> dict:
    return {"fieldName": field, "startDate": start, "endDate": end}


# ── regShoDaily (short volume) ────────────────────────────────────────────────
def _agg_short_volume(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """Aggregate regShoDaily rows (multiple per symbol/day across market codes)
    into one {short_volume,total_volume} per (ticker, trade_date)."""
    agg: dict[tuple[str, str], dict] = {}
    for row in rows:
        sym = row.get("securitiesInformationProcessorSymbolIdentifier")
        date = row.get("tradeReportDate")
        if not sym or not date:
            continue
        key = (sym, date)
        a = agg.setdefault(key, {"short": 0, "total": 0})
        a["short"] += _to_int(row.get("shortParQuantity")) or 0
        a["total"] += _to_int(row.get("totalParQuantity")) or 0
    return agg


def _short_volume_rows(agg: dict[tuple[str, str], dict], ticker: str | None = None) -> list[dict]:
    out = []
    for (sym, date), a in agg.items():
        if ticker and sym != ticker:
            continue
        total = a["total"]
        ratio = round(a["short"] / total, 4) if total else None
        out.append(
            {
                "ticker": sym,
                "trade_date": date,
                "short_volume": a["short"],
                "total_volume": total,
                "short_ratio": ratio,
            }
        )
    return out


async def _short_volume(ticker: str, days: int) -> dict:
    ticker = ticker.strip().upper()
    days = max(1, min(int(days), 120))
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days + 7)).isoformat()  # pad for lag
    cache_key = f"sv:{ticker}:{days}"
    cached = _cache_get(cache_key, TTL_FINRA)
    if cached is not None:
        return cached
    rows = await _finra_query(
        DS_REG_SHO,
        {
            "limit": 2000,
            "compareFilters": [_eq("securitiesInformationProcessorSymbolIdentifier", ticker)],
            "dateRangeFilters": [_date_range("tradeReportDate", start, today.isoformat())],
        },
    )
    agg = _agg_short_volume(rows)
    series = sorted(
        _short_volume_rows(agg, ticker), key=lambda r: r["trade_date"], reverse=True
    )[:days]
    result = {"success": True, "ticker": ticker, "days": series}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def short_volume(ticker: str, days: int = 30) -> str:
    """FINRA daily SHORT VOLUME for a ticker (covers listed names via the TRFs).

    short_volume = shares executed short that day; total_volume = all short-sale
    eligible volume reported; short_ratio = short/total. This is a FLOW, NOT
    short interest (open short shares). Returns most-recent `days` first.
    Note FINRA's consolidated file lags ~1-3 weeks.
    """
    try:
        return json.dumps(await _short_volume(ticker, days))
    except Exception as e:
        log.warning("short_volume %s failed: %s", ticker, e)
        return json.dumps({"success": False, "ticker": ticker, "error": str(e)})


async def _short_volume_day(date: str, tickers: list[str]) -> dict:
    """Bulk one-day short volume.

    IMPORTANT FINRA behavior (verified 2026-06-12): the query API caps each
    response at 5000 rows and, without an EQUAL filter on the partition key
    (tradeReportDate), returns the OLDEST date in the range -- a date-range pull
    is NOT a reliable way to get "the latest day" across the whole market.

    Strategy:
      - tickers given (the alphalens path: watchlist∪trending∪SP500) -> query
        per-ticker over a recent window (per-ticker pulls DO carry recent dates),
        aggregate, and pick the max trade_date present across the requested set.
      - no tickers + explicit date -> single EQUAL-date pull (may be partial for
        the full market given the 5000 cap; documented).
    """
    tickers = [t.strip().upper() for t in (tickers or []) if t and t.strip()]
    today = datetime.date.today()
    explicit = bool(date and date.strip())
    target = date.strip() if explicit else None
    cache_key = f"svd:{date}:{','.join(sorted(tickers))}"
    cached = _cache_get(cache_key, TTL_FINRA)
    if cached is not None:
        return cached

    all_rows: dict[tuple[str, str], dict] = {}
    if tickers:
        # Bound window: SI/RegSHO publish daily with ~1 wk lag; 12d covers it.
        start = (today - datetime.timedelta(days=12)).isoformat()
        sem = asyncio.Semaphore(1)  # ~1 rps client-side pacing (ANTIBOT §2)

        async def one(sym: str) -> None:
            async with sem:
                try:
                    rows = await _finra_query(
                        DS_REG_SHO,
                        {
                            "limit": 2000,
                            "compareFilters": [
                                _eq("securitiesInformationProcessorSymbolIdentifier", sym)
                            ],
                            "dateRangeFilters": [
                                _date_range("tradeReportDate", start, today.isoformat())
                            ],
                        },
                    )
                    for k, v in _agg_short_volume(rows).items():
                        all_rows[k] = v
                except Exception as e:
                    log.warning("short_volume_day %s leg failed: %s", sym, e)

        await asyncio.gather(*[one(s) for s in tickers])
        if target is None:
            dates = {d for (_, d) in all_rows.keys()}
            target = max(dates) if dates else None
    else:
        # Whole-market single day. Without a ticker filter we must pin the date
        # via EQUAL so FINRA returns that day (still 5000-capped -> partial).
        if target is None:
            target = (today - datetime.timedelta(days=2)).isoformat()
        rows = await _finra_query(
            DS_REG_SHO,
            {
                "limit": 5000,
                "compareFilters": [_eq("tradeReportDate", target)],
            },
        )
        all_rows = _agg_short_volume(rows)

    day_rows = [r for r in _short_volume_rows(all_rows) if r["trade_date"] == target]
    if tickers:
        tset = set(tickers)
        day_rows = [r for r in day_rows if r["ticker"] in tset]
    day_rows = [{k: v for k, v in r.items() if k != "trade_date"} for r in day_rows]
    result = {"success": True, "trade_date": target, "rows": day_rows}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def short_volume_day(date: str = "", tickers: list[str] | None = None) -> str:
    """One day of FINRA short VOLUME across symbols (bulk).

    date: ISO 'YYYY-MM-DD'; empty = latest available trade date. tickers:
    optional filter -- response contains only those symbols (server still pulls
    the day's file then filters). Each row: {ticker, short_volume, total_volume,
    short_ratio}.
    """
    try:
        return json.dumps(await _short_volume_day(date, tickers or []))
    except Exception as e:
        log.warning("short_volume_day failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── equityShortInterest (short interest snapshots; OTC-only) ──────────────────
def _si_row(row: dict) -> dict:
    return {
        "settlement_date": row.get("settlementDate"),
        "short_interest": _to_int(row.get("currentShortShareNumber")),
        "avg_daily_volume": _to_int(row.get("averageShortShareNumber")),
        "days_to_cover": _to_float(row.get("daysToCoverNumber")),
    }


async def _short_interest(ticker: str, periods: int) -> dict:
    ticker = ticker.strip().upper()
    periods = max(1, min(int(periods), 24))
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=periods * 18 + 40)).isoformat()  # SI is bi-monthly
    cache_key = f"si:{ticker}:{periods}"
    cached = _cache_get(cache_key, TTL_FINRA)
    if cached is not None:
        return cached
    rows = await _finra_query(
        DS_SHORT_INTEREST,
        {
            "limit": 500,
            "compareFilters": [_eq("issueSymbolIdentifier", ticker)],
            "dateRangeFilters": [_date_range("settlementDate", start, today.isoformat())],
        },
    )
    series = sorted(
        (_si_row(r) for r in rows),
        key=lambda r: r["settlement_date"] or "",
        reverse=True,
    )[:periods]
    result = {"success": True, "ticker": ticker, "periods": series}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def short_interest(ticker: str, periods: int = 6) -> str:
    """FINRA SHORT INTEREST snapshots for a ticker (bi-monthly settlements).

    short_interest = total open short shares at settlement; days_to_cover =
    short_interest / avg daily volume. CAVEAT: FINRA equityShortInterest covers
    OTC equities only (Rule 4560) -- exchange-listed names (NVDA/AAPL/...) are
    NOT in this dataset and return an empty list. Use short_volume() for listed
    names' short pressure.
    """
    try:
        return json.dumps(await _short_interest(ticker, periods))
    except Exception as e:
        log.warning("short_interest %s failed: %s", ticker, e)
        return json.dumps({"success": False, "ticker": ticker, "error": str(e)})


async def _short_interest_latest(tickers: list[str]) -> dict:
    tickers = [t.strip().upper() for t in (tickers or []) if t and t.strip()]
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=70)).isoformat()
    cache_key = f"sil:{','.join(sorted(tickers))}"
    cached = _cache_get(cache_key, TTL_FINRA)
    if cached is not None:
        return cached
    # Per-ticker recent-window pulls (FINRA caps responses at 5000 rows and a
    # bare date-range pull returns the oldest date, so a single bulk pull is
    # unreliable). The alphalens path always passes a bounded ticker universe.
    rows: list[dict] = []
    if tickers:
        sem = asyncio.Semaphore(1)  # ~1 rps pacing

        async def one(sym: str) -> None:
            async with sem:
                try:
                    rows.extend(
                        await _finra_query(
                            DS_SHORT_INTEREST,
                            {
                                "limit": 500,
                                "compareFilters": [_eq("issueSymbolIdentifier", sym)],
                                "dateRangeFilters": [
                                    _date_range("settlementDate", start, today.isoformat())
                                ],
                            },
                        )
                    )
                except Exception as e:
                    log.warning("short_interest_latest %s leg failed: %s", sym, e)

        await asyncio.gather(*[one(s) for s in tickers])
    else:
        rows = await _finra_query(
            DS_SHORT_INTEREST,
            {
                "limit": 5000,
                "dateRangeFilters": [_date_range("settlementDate", start, today.isoformat())],
            },
        )
    tset = set(tickers)
    latest: dict[str, dict] = {}
    for row in rows:
        sym = row.get("issueSymbolIdentifier")
        if not sym or (tset and sym not in tset):
            continue
        sd = row.get("settlementDate") or ""
        if sym not in latest or sd > (latest[sym].get("settlementDate") or ""):
            latest[sym] = row
    settlement = max((r.get("settlementDate") or "" for r in latest.values()), default=None)
    out_rows = []
    for sym, row in latest.items():
        si = _si_row(row)
        out_rows.append({"ticker": sym, **{k: v for k, v in si.items() if k != "settlement_date"}})
    result = {"success": True, "settlement_date": settlement, "rows": out_rows}
    _cache_put(cache_key, result)
    return result


@mcp.tool()
async def short_interest_latest(tickers: list[str] | None = None) -> str:
    """Latest FINRA SHORT INTEREST snapshot across symbols (bulk).

    tickers: optional filter. Each row: {ticker, short_interest, avg_daily_volume,
    days_to_cover}. CAVEAT: OTC-only (see short_interest); listed names absent.
    """
    try:
        return json.dumps(await _short_interest_latest(tickers or []))
    except Exception as e:
        log.warning("short_interest_latest failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── iBorrowDesk borrow (per-ticker JSON) ──────────────────────────────────────
async def _ibd_fetch(ticker: str) -> dict | None:
    """Fetch one ticker from iBorrowDesk. Returns a parsed dict on success,
    None when the ticker simply isn't there (404 / non-dict JSON -- a normal
    miss that must NOT count toward the circuit breaker). 403/429/5xx/timeout
    raise (these DO count toward the breaker)."""
    ticker = ticker.strip().upper()
    url = IBD_URL.format(ticker=ticker)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)
    async with _ibd_lock:
        gap = time.time() - _ibd_last_call["t"]
        if gap < _IBD_GAP_S:
            await asyncio.sleep(_IBD_GAP_S - gap)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers={"User-Agent": IBD_UA}) as r:
                text = await r.text()
                _ibd_last_call["t"] = time.time()
                if r.status == 404:
                    return None  # not on iBorrowDesk -- not a breaker failure
                if r.status != 200:
                    raise RuntimeError(f"iBorrowDesk {ticker} -> {r.status} {text[:160]}")
                stripped = text.strip()
                if not stripped:
                    return None
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"iBorrowDesk {ticker} non-JSON: {e}") from e
                if not isinstance(data, dict) or not data:
                    return None  # empty / unexpected shape -> treat as a miss
    daily = data.get("daily") or []
    rebate = _to_float(daily[-1].get("rebate")) if daily and isinstance(daily[-1], dict) else None
    return {
        "fee_rate": _to_float(data.get("latest_fee")),
        "available": _to_int(data.get("latest_available")),
        "rebate_rate": rebate,
        "updated": data.get("updated"),
    }


async def _borrow(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    cache_key = f"ibd:{ticker}"
    parsed = _cache_get(cache_key, TTL_BORROW)
    if parsed is None:
        parsed = await _ibd_fetch(ticker)
        if parsed is not None:
            _cache_put(cache_key, parsed)
    if parsed is None:
        return {"success": False, "ticker": ticker, "error": "not on iBorrowDesk"}
    return {
        "success": True,
        "ticker": ticker,
        "as_of": parsed.get("updated"),
        "fee_rate": parsed.get("fee_rate"),
        "rebate_rate": parsed.get("rebate_rate"),
        "available": parsed.get("available"),
    }


@mcp.tool()
async def borrow(ticker: str) -> str:
    """iBorrowDesk borrow snapshot for a ticker: fee_rate (annualized % to short),
    rebate_rate, and available shares. High fee_rate + low available = hard to
    borrow / squeeze risk. Cached 4h. Ticker absent from iBorrowDesk -> success
    false, error "not on iBorrowDesk".
    """
    try:
        return json.dumps(await _borrow(ticker))
    except Exception as e:
        log.warning("borrow %s failed: %s", ticker, e)
        return json.dumps({"success": False, "ticker": ticker, "error": str(e)})


async def _borrow_snapshot(tickers: list[str]) -> dict:
    tickers = [t.strip().upper() for t in (tickers or []) if t and t.strip()]
    if not tickers:
        return {
            "success": False,
            "error": "tickers required — iBorrowDesk is per-ticker (IBKR FTP bulk feed retired)",
        }
    if len(tickers) > _IBD_MAX_TICKERS:
        log.warning("borrow_snapshot: %d tickers > cap %d, truncating", len(tickers), _IBD_MAX_TICKERS)
        tickers = tickers[:_IBD_MAX_TICKERS]

    rows: list[dict] = []
    latest_updated: str | None = None
    fail_streak = 0
    partial = False
    done = 0
    for sym in tickers:
        cache_key = f"ibd:{sym}"
        parsed = _cache_get(cache_key, TTL_BORROW)
        if parsed is None:
            try:
                parsed = await _ibd_fetch(sym)
            except Exception as e:
                fail_streak += 1
                log.warning("borrow_snapshot %s leg failed (%d/%d): %s",
                            sym, fail_streak, _IBD_FAIL_STREAK_LIMIT, e)
                if fail_streak >= _IBD_FAIL_STREAK_LIMIT:
                    partial = True
                    log.warning("iBorrowDesk circuit open — %d/%d done", done, len(tickers))
                    break
                continue
            if parsed is not None:
                _cache_put(cache_key, parsed)
        done += 1
        if parsed is None:
            continue  # 404 / miss -- skip, does NOT count toward the breaker
        fail_streak = 0  # any success clears the streak
        rows.append({
            "ticker": sym,
            "fee_rate": parsed.get("fee_rate"),
            "available": parsed.get("available"),
        })
        upd = parsed.get("updated")
        if upd and (latest_updated is None or str(upd) > latest_updated):
            latest_updated = str(upd)

    as_of = latest_updated or datetime.datetime.now(datetime.timezone.utc).isoformat()
    out = {"success": True, "as_of": as_of, "rows": rows}
    if partial:
        out["partial"] = True
    return out


@mcp.tool()
async def borrow_snapshot(tickers: list[str] | None = None) -> str:
    """iBorrowDesk borrow snapshot across symbols (bulk). tickers REQUIRED
    (per-ticker API), max 80 -- excess is truncated. Each row: {ticker, fee_rate,
    available}. Cached 4h per ticker. Empty list -> success false. Tickers not on
    iBorrowDesk are skipped. On 3 consecutive hard failures the circuit opens and
    the response carries partial=true with the rows collected so far.
    """
    try:
        return json.dumps(await _borrow_snapshot(tickers or []))
    except Exception as e:
        log.warning("borrow_snapshot failed: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ── --probe CLI ────────────────────────────────────────────────────────────
async def _probe(args: list[str]) -> None:
    ticker = args[0] if args else "NVDA"
    out: dict[str, Any] = {
        "finra_creds": bool(FINRA_CLIENT_ID and FINRA_CLIENT_SECRET),
    }
    for name, coro in (
        (f"short_volume({ticker})", _short_volume(ticker, 10)),
        ("short_volume_day(latest)", _short_volume_day("", [ticker])),
        (f"short_interest({ticker})", _short_interest(ticker, 4)),
        (f"borrow({ticker})", _borrow(ticker)),
        ("borrow_snapshot", _borrow_snapshot([ticker])),
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
            "shorts MCP starting on http://%s:%s/mcp (finra creds=%s)",
            mcp.settings.host,
            mcp.settings.port,
            "set" if (FINRA_CLIENT_ID and FINRA_CLIENT_SECRET) else "anon",
        )
        from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
        serve(mcp)
