"""
stock_price_mcp.py -- yfinance-backed HTTP MCP for stock price + fundamentals.

Price / fundamentals (read-only):
  get_quote(ticker)                              current snapshot
  get_history(ticker, period, interval)          OHLCV time series
  get_info(ticker)                               sector / market cap / next earnings / dividend
  check_post_hoc(ticker, at_time, horizon)       event-study micro for KOL/call accuracy eval

Options (read-only, Tradier-backed):
  list_expirations(ticker)                       all tradeable expiry dates
  get_option_chain(ticker, expiration, ...)      chain rows + Δ/Γ/Θ/Vega/Rho
  implied_move(ticker, expiration)               ATM straddle / spot = % move priced in
  unusual_activity(ticker, expiration, ...)      strikes where volume >> open interest
  compute_greeks(ticker, expiration, strike, ...) single-strike Greeks lookup

Options data comes from the Tradier brokerage REST API (TRADIER_API_TOKEN).
yfinance's option_chain is unreliable off-hours -- we observed openInterest=0,
bid=ask=null and impliedVolatility=0 for liquid ATM strikes outside RTH. Tradier
serves broker-grade data: real open interest, full bid/ask coverage, and
Greeks (incl. mid_iv) on every contract. So `impliedVolatility` is now Tradier's
greeks.mid_iv and the Greeks subobject is Tradier's own delta/gamma/theta/vega/rho
(no more Black-Scholes recompute). _bs_greeks is kept only as a fallback for the
rare contract Tradier returns without greeks.

Price / fundamentals tools (get_quote / get_history / get_info /
check_post_hoc) still use yfinance -- they are not options data.

Runs on 127.0.0.1:3032/mcp as a daemon (At-Log-On scheduled task `StockPriceMCP`).

Caveat: yfinance scrapes Yahoo Finance. High-freq calls can hit rate limits.
Our usage (hourly market-brief + ad-hoc KOL evaluation) is well within tolerable
levels, but if you start seeing intermittent empty responses, that's the
likely cause -- wait a few minutes and retry. The Tradier REST path is rate
limited to 120 req/min (shared global lock).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import yfinance as yf
from mcp.server.fastmcp import FastMCP

# Risk-free rate used for Black-Scholes Greeks. Override per-call via the
# tool's risk_free_rate param or globally via the STOCK_MCP_RISK_FREE_RATE
# env var (decimal: 0.045 = 4.5%). Approximates the 3-month T-bill yield;
# changing it ~50bp shifts theta/rho marginally but barely touches delta.
DEFAULT_RFR = float(os.environ.get("STOCK_MCP_RISK_FREE_RATE", "0.045"))

LOG_DIR = Path(os.environ.get("STOCK_MCP_DIR") or (Path.home() / "stock-mcp")) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "stock_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("stock-mcp")

mcp = FastMCP(
    "stock-price",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("STOCK_MCP_PORT", "3032")),
)

# ── Tradier REST client (options data source) ──────────────────────────────
# Same token as tradier-mcp. yfinance options are unreliable off-hours; Tradier
# broker data has real OI / full bid-ask / Greeks. See module docstring.
TRADIER_API_TOKEN = os.environ.get("TRADIER_API_TOKEN", "")
TRADIER_REST_BASE = os.environ.get("TRADIER_REST_BASE", "https://api.tradier.com/v1")

_TRADIER_HTTP_TIMEOUT_S = 15
_TRADIER_RATE_INTERVAL_S = 60.0 / 120  # Tradier prod: 120 req/min
_tradier_last_call = 0.0
_tradier_lock = asyncio.Lock()


async def _tradier_request(path: str, params: dict | None = None) -> dict:
    """GET a Tradier REST endpoint, return parsed JSON.

    Bearer auth, JSON accept, global ~120 req/min rate limit shared across all
    option tools. Raises RuntimeError on missing token or non-200 (callers
    catch and surface {"error": ...}).
    """
    if not TRADIER_API_TOKEN:
        raise RuntimeError("TRADIER_API_TOKEN env var is not set")

    global _tradier_last_call
    async with _tradier_lock:
        wait = _TRADIER_RATE_INTERVAL_S - (time.time() - _tradier_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _tradier_last_call = time.time()

    timeout = aiohttp.ClientTimeout(total=_TRADIER_HTTP_TIMEOUT_S)
    headers = {"Authorization": f"Bearer {TRADIER_API_TOKEN}", "Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        url = f"{TRADIER_REST_BASE}/{path.lstrip('/')}"
        async with session.get(url, params=params, headers=headers) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"Tradier {path} → {r.status}: {text[:200]}")
            return json.loads(text)


async def _tradier_expirations(symbol: str) -> list[str]:
    """List option expiration dates (ascending YYYY-MM-DD) for a symbol."""
    data = await _tradier_request(
        "markets/options/expirations",
        {"symbol": symbol.upper(), "includeAllRoots": "true", "strikes": "false"},
    )
    expirations = data.get("expirations", {})
    if expirations == "null" or expirations is None:
        return []
    raw = expirations.get("date", []) if isinstance(expirations, dict) else []
    if isinstance(raw, str):  # Tradier returns a bare string when only one date
        raw = [raw]
    return list(raw)


async def _tradier_chain(symbol: str, expiration: str, greeks: bool = True) -> list[dict]:
    """Fetch the raw Tradier option chain for one expiration as a list of dicts.

    Each dict carries Tradier's native field names (strike, option_type, bid,
    ask, last, volume, open_interest, greeks{delta,gamma,theta,vega,rho,mid_iv,...}).
    """
    data = await _tradier_request(
        "markets/options/chains",
        {"symbol": symbol.upper(), "expiration": expiration, "greeks": "true" if greeks else "false"},
    )
    options = data.get("options", {})
    if options == "null" or options is None:
        return []
    raw = options.get("option", []) if isinstance(options, dict) else []
    if isinstance(raw, dict):  # single-strike response
        raw = [raw]
    return list(raw)


async def _tradier_resolve_expiration(symbol: str, expiration: str | None) -> str | None:
    """If no expiration given, return the nearest future one. Returns None if
    the symbol has no listed options, or if a given expiration isn't tradeable."""
    exps = await _tradier_expirations(symbol)
    if not exps:
        return None
    if expiration is None:
        # Tradier returns ascending; expirations are all >= today.
        return exps[0]
    return expiration if expiration in exps else None


def _f(v) -> float | None:
    """Tradier numeric → float|None (Tradier sends numbers, occasionally null)."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _tradier_spot(symbol: str) -> float:
    """Underlying last price via Tradier quote. Falls back to prevclose if the
    market is closed and `last` is null. Returns 0.0 if unavailable."""
    data = await _tradier_request("markets/quotes", {"symbols": symbol.upper(), "greeks": "false"})
    raw = data.get("quotes", {})
    if raw == "null" or raw is None:
        return 0.0
    q = raw.get("quote") if isinstance(raw, dict) else None
    if isinstance(q, list):
        q = q[0] if q else None
    if not isinstance(q, dict):
        return 0.0
    return _f(q.get("last")) or _f(q.get("close")) or _f(q.get("prevclose")) or 0.0


HORIZON_TO_DELTA = {
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(days=7),
    "2w": timedelta(days=14),
    "1mo": timedelta(days=30),
}


def _err(ticker: str, exc: Exception, ctx: str = "") -> dict:
    log.exception("%s %s failed", ctx, ticker)
    return {"error": f"{type(exc).__name__}: {exc}", "ticker": ticker}


@mcp.tool()
async def get_quote(ticker: str) -> dict:
    """Current snapshot for a US ticker (price/change/volume/market cap/52w range)."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        if price is None:
            h = t.history(period="1d", interval="1m")
            if not h.empty:
                price = float(h.iloc[-1].Close)
        out = {
            "ticker": ticker.upper(),
            "name": info.get("shortName") or info.get("longName"),
            "price": price,
            "change": info.get("regularMarketChange"),
            "change_pct": info.get("regularMarketChangePercent"),
            "volume": info.get("regularMarketVolume") or info.get("volume"),
            "market_cap": info.get("marketCap"),
            "day_high": info.get("dayHigh"),
            "day_low": info.get("dayLow"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "prev_close": info.get("regularMarketPreviousClose"),
            "currency": info.get("currency"),
            "exchange": info.get("exchange"),
        }
        log.info("get_quote %s -> $%s", ticker, price)
        return out
    except Exception as e:
        return _err(ticker, e, "get_quote")


@mcp.tool()
async def get_history(
    ticker: str,
    period: str = "1mo",
    interval: str = "1d",
) -> dict:
    """OHLCV history. Caps to most recent 50 bars to keep response small.

    period: 1d / 5d / 1mo / 3mo / 6mo / 1y / 2y / 5y / max
    interval: 1m / 5m / 15m / 30m / 1h / 1d / 1wk / 1mo
       (intraday <1d only available for the last 7-60 days; yfinance enforces)
    """
    try:
        t = yf.Ticker(ticker)
        h = t.history(period=period, interval=interval)
        if h.empty:
            return {"error": "no data; check ticker / period / interval combo",
                    "ticker": ticker, "period": period, "interval": interval}
        h = h.tail(50)
        bars = []
        for idx, row in h.iterrows():
            bars.append({
                "date": idx.isoformat() if hasattr(idx, "isoformat") else str(idx),
                "open": round(float(row.Open), 4),
                "high": round(float(row.High), 4),
                "low": round(float(row.Low), 4),
                "close": round(float(row.Close), 4),
                "volume": int(row.Volume) if row.Volume == row.Volume else None,  # NaN check
            })
        log.info("get_history %s period=%s int=%s -> %d bars", ticker, period, interval, len(bars))
        return {
            "ticker": ticker.upper(),
            "period": period,
            "interval": interval,
            "bar_count": len(bars),
            "bars": bars,
        }
    except Exception as e:
        return _err(ticker, e, "get_history")


@mcp.tool()
async def get_info(ticker: str) -> dict:
    """Fundamentals + key dates. Use to check earnings calendar before betting."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        next_earnings = None
        try:
            cal = t.calendar
            if cal is not None and "Earnings Date" in cal:
                d = cal["Earnings Date"]
                if d:
                    next_earnings = d[0].isoformat() if hasattr(d[0], "isoformat") else str(d[0])
        except Exception:
            pass
        ex_div = info.get("exDividendDate")
        if isinstance(ex_div, (int, float)):
            try:
                ex_div = datetime.fromtimestamp(ex_div, tz=timezone.utc).date().isoformat()
            except Exception:
                pass
        return {
            "ticker": ticker.upper(),
            "name": info.get("shortName") or info.get("longName"),
            # yfinance quoteType is the canonical instrument-type tag:
            # 'EQUITY' / 'ETF' / 'MUTUALFUND' / 'INDEX' / 'CURRENCY' /
            # 'CRYPTOCURRENCY'. alphalens uses this to gate cluster
            # membership instead of maintaining a hand-curated ETF set
            # (which kept missing new ARK/SPDR/PIMCO names).
            "quote_type": info.get("quoteType"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "market_cap": info.get("marketCap"),
            "forward_pe": info.get("forwardPE"),
            "trailing_pe": info.get("trailingPE"),
            "dividend_yield": info.get("dividendYield"),
            # Phase 5M-3 — analyst-driven growth proxy (most recent quarter
            # YoY EPS growth). Best free signal without paid consensus feed.
            "earnings_growth": info.get("earningsGrowth"),
            "revenue_growth": info.get("revenueGrowth"),
            "payout_ratio": info.get("payoutRatio"),
            "beta": info.get("beta"),
            "employees": info.get("fullTimeEmployees"),
            "next_earnings_date": next_earnings,
            "ex_dividend_date": ex_div,
            "fifty_day_avg": info.get("fiftyDayAverage"),
            "two_hundred_day_avg": info.get("twoHundredDayAverage"),
            "short_ratio": info.get("shortRatio"),
            "short_percent_of_float": info.get("shortPercentOfFloat"),
            "summary": (info.get("longBusinessSummary") or "")[:500],
        }
    except Exception as e:
        return _err(ticker, e, "get_info")


@mcp.tool()
async def check_post_hoc(
    ticker: str,
    at_time: str,
    horizon: str = "1d",
) -> dict:
    """Event-study micro for evaluating a KOL/analyst call accuracy.

    Look at price near `at_time` (e.g. a tweet's timestamp) and N period later,
    plus max/min reached inside the window.

    Args:
      ticker: e.g. 'TSLA'
      at_time: ISO datetime, UTC preferred. Examples:
               '2026-05-15T14:30:00Z'  '2026-05-15T14:30:00+00:00'
      horizon: 1h / 4h / 1d / 3d / 1w / 2w / 1mo

    Returns price_at_time, price_at_horizon, max/min reached, gain%/drawdown%/net%.
    If market was closed during the window (e.g. weekend tweet, short horizon),
    returns an error -- caller should retry with longer horizon.
    """
    try:
        try:
            at_dt = datetime.fromisoformat(at_time.replace("Z", "+00:00"))
        except ValueError:
            return {"error": f"unparseable at_time {at_time!r}; use ISO 8601"}
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=timezone.utc)
        else:
            at_dt = at_dt.astimezone(timezone.utc)

        delta = HORIZON_TO_DELTA.get(horizon)
        if delta is None:
            return {"error": f"unknown horizon {horizon!r}",
                    "valid_horizons": list(HORIZON_TO_DELTA.keys())}

        end_dt = at_dt + delta
        interval = "1h" if delta <= timedelta(days=1) else "1d"

        # yfinance start/end as date strings; pad to catch edges
        start = (at_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        end_pad = (end_dt + timedelta(days=2)).strftime("%Y-%m-%d")

        t = yf.Ticker(ticker)
        h = t.history(start=start, end=end_pad, interval=interval)
        if h.empty:
            return {"error": "no bars (ticker invalid? date too far back? market closed?)",
                    "ticker": ticker, "at_time": at_dt.isoformat(), "horizon": horizon}

        if h.index.tz is not None:
            h.index = h.index.tz_convert("UTC")

        in_window = h[(h.index >= at_dt) & (h.index <= end_dt)]
        if in_window.empty:
            return {"error": "no bars within [at_time, at_time+horizon] -- "
                             "market closed during window?",
                    "ticker": ticker, "at_time": at_dt.isoformat(), "horizon": horizon,
                    "available_window": [h.index[0].isoformat(), h.index[-1].isoformat()]}

        first_bar = in_window.iloc[0]
        last_bar = in_window.iloc[-1]
        price_at = float(first_bar.Open)
        price_at_horizon = float(last_bar.Close)
        max_high = float(in_window.High.max())
        min_low = float(in_window.Low.min())

        log.info("check_post_hoc %s at=%s horizon=%s -> net %.2f%%",
                 ticker, at_dt.isoformat(), horizon,
                 (price_at_horizon - price_at) / price_at * 100)

        return {
            "ticker": ticker.upper(),
            "at_time": at_dt.isoformat(),
            "horizon": horizon,
            "interval_used": interval,
            "bar_count": len(in_window),
            "price_at_time": round(price_at, 4),
            "price_at_horizon": round(price_at_horizon, 4),
            "max_high": round(max_high, 4),
            "min_low": round(min_low, 4),
            "max_gain_pct": round((max_high - price_at) / price_at * 100, 2),
            "max_drawdown_pct": round((min_low - price_at) / price_at * 100, 2),
            "net_move_pct": round((price_at_horizon - price_at) / price_at * 100, 2),
        }
    except Exception as e:
        return _err(ticker, e, "check_post_hoc")


# ───────────────────────────── Options + Greeks ────────────────────────────


def _norm_cdf(x: float) -> float:
    """N(x): standard-normal CDF via erf. Stdlib only (no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """N'(x): standard-normal PDF."""
    return math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)


def _bs_greeks(
    S: float, K: float, T: float, r: float, sigma: float, opt_type: str
) -> dict:
    """Black-Scholes Greeks for a European call/put on a non-dividend stock.

    Returns delta/gamma/theta(per day)/vega(per 1% IV)/rho(per 1% rate).
    None values if any input is degenerate (T<=0, sigma<=0, S<=0, K<=0).

    Kept as a fallback only: options Greeks now come straight from Tradier
    (greeks subobject + mid_iv). This is used when Tradier returns a contract
    with no greeks block (rare).
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "rho": None}

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf_d1 = _norm_pdf(d1)

    if opt_type == "call":
        delta = _norm_cdf(d1)
        theta = (
            -S * pdf_d1 * sigma / (2.0 * sqrtT)
            - r * K * math.exp(-r * T) * _norm_cdf(d2)
        ) / 365.0
        rho = K * T * math.exp(-r * T) * _norm_cdf(d2) / 100.0
    else:  # put
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -S * pdf_d1 * sigma / (2.0 * sqrtT)
            + r * K * math.exp(-r * T) * _norm_cdf(-d2)
        ) / 365.0
        rho = -K * T * math.exp(-r * T) * _norm_cdf(-d2) / 100.0

    gamma = pdf_d1 / (S * sigma * sqrtT)
    vega = S * pdf_d1 * sqrtT / 100.0  # per 1% IV change (sigma unit = decimal)

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 5),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
        "rho": round(rho, 4),
    }


def _years_to_expiry(expiration: str) -> float:
    """ISO date 'YYYY-MM-DD' (UTC) -> fractional years from now."""
    try:
        exp_dt = datetime.strptime(expiration, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return -1.0
    # Options resolve at end of trading day; tack on ~16:00 ET = ~20:00 UTC
    exp_dt = exp_dt.replace(hour=20)
    delta = exp_dt - datetime.now(timezone.utc)
    return delta.total_seconds() / (365.0 * 86400.0)


def _slim_row(o: dict, spot: float, T: float, r: float, opt_type: str,
              include_greeks: bool) -> dict:
    """Reduce one Tradier option dict to a brief-friendly dict + optional
    Greeks. Schema is identical to the previous yfinance version (consumers
    rely on these exact field names): Tradier `open_interest` -> openInterest,
    greeks.mid_iv -> impliedVolatility, `last` -> lastPrice, others verbatim.

    Greeks come straight from Tradier; if Tradier omits the greeks block we
    fall back to Black-Scholes from mid_iv."""
    strike = _f(o.get("strike")) or 0.0
    g = o.get("greeks") or {}
    mid_iv = _f(g.get("mid_iv"))
    out = {
        "strike": strike,
        "lastPrice": _f(o.get("last")),
        "bid": _f(o.get("bid")),
        "ask": _f(o.get("ask")),
        "volume": int(o.get("volume") or 0),
        "openInterest": int(o.get("open_interest") or 0),
        "impliedVolatility": round(mid_iv, 4) if mid_iv else None,
        # Tradier doesn't ship an inTheMoney flag; derive it from spot vs strike.
        "inTheMoney": (
            (spot > strike) if opt_type == "call" else (spot < strike)
        ) if spot and strike else None,
    }
    if include_greeks:
        if g:
            out["greeks"] = {
                "delta": _f(g.get("delta")),
                "gamma": _f(g.get("gamma")),
                "theta": _f(g.get("theta")),
                "vega": _f(g.get("vega")),
                "rho": _f(g.get("rho")),
            }
        elif mid_iv:  # fallback: Tradier gave no greeks but we have an IV
            out["greeks"] = _bs_greeks(spot, strike, T, r, mid_iv, opt_type)
    return out


@mcp.tool()
async def list_expirations(ticker: str) -> dict:
    """Return all tradeable option expiration dates for a ticker.

    Returns ISO dates sorted ascending. Use this before calling
    get_option_chain or implied_move to know what expirations exist.
    """
    try:
        opts = await _tradier_expirations(ticker)
        log.info("list_expirations %s -> %d expirations", ticker, len(opts))
        return {"ticker": ticker.upper(), "count": len(opts), "expirations": opts}
    except Exception as e:
        return _err(ticker, e, "list_expirations")


@mcp.tool()
async def get_option_chain(
    ticker: str,
    expiration: str = "",
    contract_type: str = "both",
    near_strike_pct: float = 10.0,
    limit: int = 20,
    with_greeks: bool = True,
    risk_free_rate: float = 0.0,
) -> dict:
    """Option chain for ticker @ expiration, filtered to strikes near spot.

    expiration: ISO date 'YYYY-MM-DD'. If "", uses the nearest in the future.
    contract_type: 'calls' | 'puts' | 'both'.
    near_strike_pct: keep strikes within ±this% of spot. Pass 100 for full chain.
    limit: max rows per type after filtering (sorted by closeness to spot).
    with_greeks: when True (default) each row gets a `greeks` subobject
                 (Δ/Γ/Θ/Vega/Rho) straight from Tradier. Set False to omit.
    risk_free_rate: decimal (0.045 = 4.5%). Only used for the Black-Scholes
                    fallback when Tradier omits a contract's greeks.

    Returns calls/puts arrays, sorted by strike ascending.
    """
    try:
        exp = await _tradier_resolve_expiration(ticker, expiration or None)
        if exp is None:
            return _err(ticker, ValueError(f"no options or unknown expiration {expiration!r}"),
                        "get_option_chain")

        spot = await _tradier_spot(ticker)
        if spot <= 0:
            return _err(ticker, ValueError("could not determine spot price"),
                        "get_option_chain")
        T = _years_to_expiry(exp)
        if T <= 0:
            return _err(ticker, ValueError(f"expiration {exp} is in the past"),
                        "get_option_chain")
        r = risk_free_rate if risk_free_rate > 0 else DEFAULT_RFR

        options = await _tradier_chain(ticker, exp, greeks=with_greeks)
        lo = spot * (1 - near_strike_pct / 100.0)
        hi = spot * (1 + near_strike_pct / 100.0)

        def _build(opt_type: str) -> list[dict]:
            # Tradier option_type is 'call' / 'put'.
            rows = [o for o in options
                    if (o.get("option_type") == opt_type
                        and (_f(o.get("strike")) or 0) >= lo
                        and (_f(o.get("strike")) or 0) <= hi)]
            rows.sort(key=lambda o: abs((_f(o.get("strike")) or 0) - spot))
            rows = rows[:limit]
            slim = [_slim_row(o, spot, T, r, opt_type, with_greeks) for o in rows]
            slim.sort(key=lambda d: d["strike"])
            return slim

        result = {
            "ticker": ticker.upper(),
            "expiration": exp,
            "spot": round(spot, 4),
            "days_to_expiry": round(T * 365, 2),
            "risk_free_rate": r,
        }
        if contract_type in ("calls", "both"):
            result["calls"] = _build("call")
        if contract_type in ("puts", "both"):
            result["puts"] = _build("put")
        log.info("get_option_chain %s %s near=%g%% -> %d calls / %d puts",
                 ticker, exp, near_strike_pct,
                 len(result.get("calls", [])), len(result.get("puts", [])))
        return result
    except Exception as e:
        return _err(ticker, e, "get_option_chain")


@mcp.tool()
async def implied_move(ticker: str, expiration: str = "") -> dict:
    """ATM straddle price / spot = implied % move by expiration.

    This is the single most useful options-derived number for catalyst prep
    (earnings, FOMC, etc.). Says "the options market is pricing in a ±X.X%
    move by <date>".

    expiration: ISO 'YYYY-MM-DD'. If "", uses the nearest in the future.

    Computed: pick the strike closest to spot, average its call + put
    mid-prices (bid/ask average, else last), divide by spot.
    """
    try:
        exp = await _tradier_resolve_expiration(ticker, expiration or None)
        if exp is None:
            return _err(ticker, ValueError("no options available"), "implied_move")
        spot = await _tradier_spot(ticker)
        if spot <= 0:
            return _err(ticker, ValueError("could not determine spot"), "implied_move")
        options = await _tradier_chain(ticker, exp, greeks=True)

        def _atm(opt_type: str) -> dict | None:
            rows = [o for o in options if o.get("option_type") == opt_type
                    and _f(o.get("strike")) is not None]
            if not rows:
                return None
            return min(rows, key=lambda o: abs((_f(o.get("strike")) or 0) - spot))

        c = _atm("call")
        p = _atm("put")
        if c is None or p is None:
            return _err(ticker, ValueError("no ATM call/put in chain"), "implied_move")

        def _mid(o: dict) -> float:
            bid, ask = _f(o.get("bid")), _f(o.get("ask"))
            if bid and ask:
                return (bid + ask) / 2.0
            return _f(o.get("last")) or 0.0

        def _iv(o: dict) -> float | None:
            return _f((o.get("greeks") or {}).get("mid_iv"))

        call_mid = _mid(c)
        put_mid = _mid(p)
        straddle = call_mid + put_mid
        move_pct = straddle / spot * 100.0
        T = _years_to_expiry(exp)

        log.info("implied_move %s %s -> ±%.2f%%", ticker, exp, move_pct)
        return {
            "ticker": ticker.upper(),
            "expiration": exp,
            "days_to_expiry": round(T * 365, 2),
            "spot": round(spot, 4),
            "atm_call_strike": _f(c.get("strike")),
            "atm_put_strike": _f(p.get("strike")),
            "call_mid": round(call_mid, 4),
            "put_mid": round(put_mid, 4),
            "straddle": round(straddle, 4),
            "implied_move_pct": round(move_pct, 3),
            "implied_move_abs": round(straddle, 4),
            "upper_breakeven": round(spot + straddle, 4),
            "lower_breakeven": round(spot - straddle, 4),
            "atm_iv_call": round(_iv(c), 4) if _iv(c) else None,
            "atm_iv_put": round(_iv(p), 4) if _iv(p) else None,
        }
    except Exception as e:
        return _err(ticker, e, "implied_move")


@mcp.tool()
async def unusual_activity(
    ticker: str,
    expiration: str = "",
    min_vol_oi_ratio: float = 3.0,
    min_volume: int = 100,
    limit: int = 20,
) -> dict:
    """Strikes with volume substantially above open interest.

    Use case: cross-check group/KOL bullish or bearish calls -- if "$240
    NVDA call is exploding" is real, this tool surfaces it. Filters for
    volume/OI >= min_vol_oi_ratio AND volume >= min_volume so we don't
    flag low-liquidity strikes that just happened to print once.

    expiration: ISO 'YYYY-MM-DD'. If "", uses the nearest in the future.

    Returns calls + puts, each sorted by vol/OI ratio descending. Open interest
    is now Tradier's real broker OI (yfinance reported OI=0 off-hours, which
    made every strike look unusual).
    """
    try:
        exp = await _tradier_resolve_expiration(ticker, expiration or None)
        if exp is None:
            return _err(ticker, ValueError("no options available"), "unusual_activity")
        spot = await _tradier_spot(ticker)
        options = await _tradier_chain(ticker, exp, greeks=True)

        def _filter_unusual(opt_type: str) -> list[dict]:
            rows = []
            for o in options:
                if o.get("option_type") != opt_type:
                    continue
                vol = int(o.get("volume") or 0)
                oi = int(o.get("open_interest") or 0)
                # Treat OI=0 as 1 to avoid div-by-zero; that's still "unusual"
                # if volume is high. (Matches the prior yfinance behavior; with
                # Tradier real OI, genuine zero-OI strikes are now rare.)
                ratio = vol / max(oi, 1)
                if vol >= min_volume and ratio >= min_vol_oi_ratio:
                    rows.append((o, vol, oi, ratio))
            rows.sort(key=lambda x: x[3], reverse=True)
            return rows[:limit]

        def _row_to_dict(item, opt_type):
            o, vol, oi, ratio = item
            strike = _f(o.get("strike")) or 0.0
            mid_iv = _f((o.get("greeks") or {}).get("mid_iv"))
            return {
                "strike": strike,
                "contract_type": opt_type,
                "volume": vol,
                "openInterest": oi,
                "vol_oi_ratio": round(ratio, 2),
                "lastPrice": _f(o.get("last")),
                "bid": _f(o.get("bid")),
                "ask": _f(o.get("ask")),
                "impliedVolatility": round(mid_iv, 4) if mid_iv else None,
                "moneyness_pct": (
                    round((strike / spot - 1) * 100, 2) if spot else None
                ),
            }

        unusual_calls = [_row_to_dict(it, "call") for it in _filter_unusual("call")]
        unusual_puts = [_row_to_dict(it, "put") for it in _filter_unusual("put")]
        log.info("unusual_activity %s %s -> %d calls / %d puts",
                 ticker, exp, len(unusual_calls), len(unusual_puts))
        return {
            "ticker": ticker.upper(),
            "expiration": exp,
            "spot": round(spot, 4),
            "filter": f"vol/OI >= {min_vol_oi_ratio} AND vol >= {min_volume}",
            "unusual_calls": unusual_calls,
            "unusual_puts": unusual_puts,
        }
    except Exception as e:
        return _err(ticker, e, "unusual_activity")


@mcp.tool()
async def compute_greeks(
    ticker: str,
    expiration: str,
    strike: float,
    contract_type: str = "call",
    risk_free_rate: float = 0.0,
) -> dict:
    """Single-strike Greeks lookup. Returns Δ/Γ/Θ/Vega/Rho + the inputs
    they were computed from.

    expiration: ISO 'YYYY-MM-DD'.
    strike: exact strike (must exist in the chain).
    contract_type: 'call' or 'put'.
    risk_free_rate: decimal, 0 takes the module default (4.5%). Only used for
                    the Black-Scholes fallback when Tradier omits greeks.

    Greeks + impliedVolatility (mid_iv) come straight from Tradier.
    """
    if contract_type not in ("call", "put"):
        return _err(ticker, ValueError("contract_type must be 'call' or 'put'"),
                    "compute_greeks")
    try:
        exps = await _tradier_expirations(ticker)
        if expiration not in exps:
            return _err(ticker, ValueError(f"unknown expiration {expiration!r}"),
                        "compute_greeks")
        spot = await _tradier_spot(ticker)
        if spot <= 0:
            return _err(ticker, ValueError("could not determine spot"), "compute_greeks")
        T = _years_to_expiry(expiration)
        if T <= 0:
            return _err(ticker, ValueError("expiration is in the past"), "compute_greeks")
        r = risk_free_rate if risk_free_rate > 0 else DEFAULT_RFR

        options = await _tradier_chain(ticker, expiration, greeks=True)
        same_type = [o for o in options if o.get("option_type") == contract_type
                     and _f(o.get("strike")) is not None]
        matches = [o for o in same_type if _f(o.get("strike")) == strike]
        if not matches:
            available = sorted(_f(o.get("strike")) for o in same_type)
            nearest = min(available, key=lambda s: abs(s - strike)) if available else None
            return _err(ticker,
                        ValueError(f"strike {strike} not in chain. Nearest: {nearest}"),
                        "compute_greeks")
        o = matches[0]
        g = o.get("greeks") or {}
        iv = _f(g.get("mid_iv")) or 0.0
        if g:  # Tradier greeks (preferred)
            greeks = {
                "delta": _f(g.get("delta")),
                "gamma": _f(g.get("gamma")),
                "theta": _f(g.get("theta")),
                "vega": _f(g.get("vega")),
                "rho": _f(g.get("rho")),
            }
        else:  # fallback: Black-Scholes from mid_iv
            greeks = _bs_greeks(spot, strike, T, r, iv, contract_type)
        log.info("compute_greeks %s %s %s %s -> Δ=%s", ticker, expiration, strike,
                 contract_type, greeks.get("delta"))
        return {
            "ticker": ticker.upper(),
            "expiration": expiration,
            "strike": strike,
            "contract_type": contract_type,
            "spot": round(spot, 4),
            "days_to_expiry": round(T * 365, 2),
            "risk_free_rate": r,
            "impliedVolatility": round(iv, 4) if iv else None,
            "bid": _f(o.get("bid")),
            "ask": _f(o.get("ask")),
            "lastPrice": _f(o.get("last")),
            "volume": int(o.get("volume") or 0),
            "openInterest": int(o.get("open_interest") or 0),
            "greeks": greeks,
        }
    except Exception as e:
        return _err(ticker, e, "compute_greeks")


if __name__ == "__main__":
    log.info("stock-price MCP starting on http://127.0.0.1:%s/mcp", mcp.settings.port)
    mcp.run(transport="streamable-http")
