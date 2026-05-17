"""
stock_price_mcp.py -- yfinance-backed HTTP MCP for stock price + fundamentals.

Tools (all read-only):
  get_quote(ticker)                              current snapshot
  get_history(ticker, period, interval)          OHLCV time series
  get_info(ticker)                               sector / market cap / next earnings / dividend
  check_post_hoc(ticker, at_time, horizon)       event-study micro for KOL/call accuracy eval

Runs on 127.0.0.1:3032/mcp as a daemon (At-Log-On scheduled task `StockPriceMCP`).

Caveat: yfinance scrapes Yahoo Finance. High-freq calls can hit rate limits.
Our usage (hourly market-brief + ad-hoc KOL evaluation) is well within tolerable
levels, but if you start seeing intermittent empty responses, that's the
likely cause -- wait a few minutes and retry.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf
from mcp.server.fastmcp import FastMCP

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
    host="127.0.0.1",
    port=int(os.environ.get("STOCK_MCP_PORT", "3032")),
)


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
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "country": info.get("country"),
            "market_cap": info.get("marketCap"),
            "forward_pe": info.get("forwardPE"),
            "trailing_pe": info.get("trailingPE"),
            "dividend_yield": info.get("dividendYield"),
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


if __name__ == "__main__":
    log.info("stock-price MCP starting on http://127.0.0.1:%s/mcp", mcp.settings.port)
    mcp.run(transport="streamable-http")
