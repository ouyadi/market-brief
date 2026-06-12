"""defeatbeta_mcp.py -- thin FastMCP wrapper over the defeatbeta-api library.

defeatbeta-api (Apache-2.0) is an open-source Yahoo-Finance alternative backed
by a Hugging Face dataset queried through DuckDB. It gives earnings-call
transcripts and structured financial statements for free -- a fallback for the
SeekingAlpha transcript/financials tools when SA is rate-limited or blocked.

DATA IS WEEKLY-REFRESHED: the most recent quarter/transcript may be missing for
a few days after the event. Callers should treat it as a ~1-week-delayed source.

First access for a symbol pulls parquet shards from Hugging Face (slow, ~seconds);
results are cached in-process. The HF cache dir is DEFEATBETA_CACHE_DIR (default
~/defeatbeta-cache). No API key needed.

Tools:
  transcripts_list(ticker)                 available earnings-call transcripts
  transcript(ticker, year, quarter)        one transcript, body capped 60k chars
  financials(ticker, statement, period, periods)   income/balance/cashflow table

Run as HTTP MCP on 127.0.0.1:3042/mcp by default. Override port via
DEFEATBETA_MCP_PORT env var.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# Point the library's HF cache at a configurable dir (default ~/defeatbeta-cache).
# Several HF/duckdb knobs read these env vars; set before importing the library.
_CACHE_DIR = os.environ.get("DEFEATBETA_CACHE_DIR") or str(Path.home() / "defeatbeta-cache")
os.environ.setdefault("HF_HOME", _CACHE_DIR)
os.environ.setdefault("HF_DATASETS_CACHE", _CACHE_DIR)
Path(_CACHE_DIR).mkdir(parents=True, exist_ok=True)

from mcp.server.fastmcp import FastMCP

LOG_DIR = (
    Path(os.environ.get("DEFEATBETA_MCP_DIR") or (Path.home() / "defeatbeta-mcp"))
    / "logs"
)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_DIR / "defeatbeta_mcp.log", encoding="utf-8")],
)
log = logging.getLogger("defeatbeta")

TRANSCRIPT_CHAR_CAP = 60_000  # mirror sec-edgar's body cap

mcp = FastMCP(
    "defeatbeta",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("DEFEATBETA_MCP_PORT", "3042")),
)

# ── in-process cache (the parquet pulls are expensive) ───────────────────────
_CACHE: dict[str, tuple[float, Any]] = {}
TTL = 12 * 60 * 60  # 12h; data is weekly-refreshed so this is plenty fresh
_ticker_cache: dict[str, Any] = {}


def _cache_get(key: str) -> Any | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > TTL:
        return None
    return val


def _cache_put(key: str, val: Any) -> None:
    _CACHE[key] = (time.time(), val)


def _get_ticker(symbol: str):
    """Lazy-import + memoize a Ticker. Import is deferred so the module loads
    (and --help/port binding works) even if the heavy deps hiccup."""
    from defeatbeta_api.data.ticker import Ticker  # noqa: PLC0415

    sym = symbol.strip().upper()
    if sym not in _ticker_cache:
        _ticker_cache[sym] = Ticker(sym)
    return _ticker_cache[sym]


# ── transcripts_list ──────────────────────────────────────────────────────────
def _transcripts_list_blocking(ticker: str) -> dict:
    t = _get_ticker(ticker)
    df = t.earning_call_transcripts().get_transcripts_list()
    rows = []
    for _, r in df.iterrows():
        try:
            year = int(r.get("fiscal_year"))
            quarter = int(r.get("fiscal_quarter"))
        except (TypeError, ValueError):
            continue
        rows.append({"year": year, "quarter": quarter, "date": str(r.get("report_date"))})
    rows.sort(key=lambda x: (x["year"], x["quarter"]), reverse=True)
    return {"success": True, "ticker": ticker.strip().upper(), "transcripts": rows}


async def _transcripts_list(ticker: str) -> dict:
    key = f"tl:{ticker.strip().upper()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(_transcripts_list_blocking, ticker)
    _cache_put(key, result)
    return result


@mcp.tool()
async def transcripts_list(ticker: str) -> str:
    """List available earnings-call transcripts for a ticker (newest first):
    {year, quarter, date}. WEEKLY-refreshed source -- the latest quarter may be
    missing for a few days after the call. Fallback for the SA transcripts tool.
    """
    try:
        return json.dumps(await _transcripts_list(ticker))
    except Exception as e:
        log.warning("transcripts_list %s failed: %s", ticker, e)
        return json.dumps({"success": False, "ticker": ticker, "error": str(e)})


# ── transcript ────────────────────────────────────────────────────────────────
def _transcript_blocking(ticker: str, year: int, quarter: int) -> dict:
    t = _get_ticker(ticker)
    df = t.earning_call_transcripts().get_transcript(int(year), int(quarter))
    paragraphs = []
    char_count = 0
    truncated = False
    for _, r in df.iterrows():
        text = str(r.get("content") or "")
        speaker = str(r.get("speaker") or "")
        if char_count + len(text) > TRANSCRIPT_CHAR_CAP:
            # include a final partial paragraph up to the cap, then stop
            remaining = TRANSCRIPT_CHAR_CAP - char_count
            if remaining > 0:
                paragraphs.append({"speaker": speaker, "text": text[:remaining]})
                char_count += remaining
            truncated = True
            break
        paragraphs.append({"speaker": speaker, "text": text})
        char_count += len(text)
    return {
        "success": True,
        "ticker": ticker.strip().upper(),
        "year": int(year),
        "quarter": int(quarter),
        "paragraphs": paragraphs,
        "char_count": char_count,
        "truncated": truncated,
    }


async def _transcript(ticker: str, year: int, quarter: int) -> dict:
    key = f"t:{ticker.strip().upper()}:{year}:{quarter}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(_transcript_blocking, ticker, year, quarter)
    _cache_put(key, result)
    return result


@mcp.tool()
async def transcript(ticker: str, year: int, quarter: int) -> str:
    """Full earnings-call transcript for a ticker/fiscal year/quarter.

    Returns paragraphs: [{speaker, text}], char_count, and truncated (body is
    capped at 60k chars like the sec-edgar filing tool). WEEKLY-refreshed source.
    Use transcripts_list() first to see which (year, quarter) are available.
    """
    try:
        return json.dumps(await _transcript(ticker, year, quarter))
    except Exception as e:
        log.warning("transcript %s %s/%s failed: %s", ticker, year, quarter, e)
        return json.dumps(
            {"success": False, "ticker": ticker, "year": year, "quarter": quarter, "error": str(e)}
        )


# ── financials ────────────────────────────────────────────────────────────────
_STMT_METHODS = {
    ("income", "annual"): "annual_income_statement",
    ("income", "quarterly"): "quarterly_income_statement",
    ("balance", "annual"): "annual_balance_sheet",
    ("balance", "quarterly"): "quarterly_balance_sheet",
    ("cash_flow", "annual"): "annual_cash_flow",
    ("cash_flow", "quarterly"): "quarterly_cash_flow",
    # accept a couple of aliases for the statement name
    ("cashflow", "annual"): "annual_cash_flow",
    ("cashflow", "quarterly"): "quarterly_cash_flow",
    ("balance_sheet", "annual"): "annual_balance_sheet",
    ("balance_sheet", "quarterly"): "quarterly_balance_sheet",
}


def _financials_blocking(ticker: str, statement: str, period: str, periods: int) -> dict:
    statement = (statement or "income").strip().lower()
    period = (period or "annual").strip().lower()
    periods = max(1, min(int(periods), 20))
    method_name = _STMT_METHODS.get((statement, period))
    if not method_name:
        raise RuntimeError(
            f"unknown statement/period: {statement}/{period} "
            "(statement: income|balance|cash_flow, period: annual|quarterly)"
        )
    t = _get_ticker(ticker)
    stmt = getattr(t, method_name)()
    df = stmt.data  # DataFrame: first col 'Breakdown' (line item) + period columns
    cols = list(df.columns)
    label_col = cols[0]  # 'Breakdown'
    # period columns are everything after the label; keep most recent `periods`.
    # The library emits TTM first then dates descending -> first N are newest.
    period_cols = cols[1 : 1 + periods]
    rows = []
    for _, r in df.iterrows():
        name = str(r.get(label_col) or "").strip()
        if not name:
            continue
        cells = []
        for pc in period_cols:
            val = r.get(pc)
            # JSON-safe: pandas/numpy scalars -> python; NaN -> None
            try:
                import math

                if val is None or (isinstance(val, float) and math.isnan(val)):
                    val = None
                elif hasattr(val, "item"):
                    val = val.item()
            except Exception:
                val = None if val is None else str(val)
            cells.append({"period_label": str(pc), "value": val})
        rows.append({"name": name, "cells": cells})
    return {
        "success": True,
        "ticker": ticker.strip().upper(),
        "statement": statement,
        "period": period,
        "rows": rows,
    }


async def _financials(ticker: str, statement: str, period: str, periods: int) -> dict:
    key = f"f:{ticker.strip().upper()}:{statement}:{period}:{periods}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(_financials_blocking, ticker, statement, period, periods)
    _cache_put(key, result)
    return result


@mcp.tool()
async def financials(
    ticker: str, statement: str = "income", period: str = "annual", periods: int = 5
) -> str:
    """Structured financial statement for a ticker.

    statement: 'income' | 'balance' | 'cash_flow'. period: 'annual' | 'quarterly'.
    periods: how many trailing periods (1-20, default 5). Returns rows:
    [{name (line item), cells: [{period_label, value}]}], most-recent first.
    WEEKLY-refreshed source -- the latest period may lag.
    """
    try:
        return json.dumps(await _financials(ticker, statement, period, periods))
    except Exception as e:
        log.warning("financials %s %s/%s failed: %s", ticker, statement, period, e)
        return json.dumps({"success": False, "ticker": ticker, "error": str(e)})


# ── --probe CLI ────────────────────────────────────────────────────────────
async def _probe(args: list[str]) -> None:
    ticker = args[0] if args else "AAPL"
    out: dict[str, Any] = {}
    try:
        tl = await _transcripts_list(ticker)
        out["transcripts_list"] = {
            "success": tl.get("success"),
            "n": len(tl.get("transcripts", [])),
            "latest": tl.get("transcripts", [None])[0],
        }
        if tl.get("transcripts"):
            latest = tl["transcripts"][0]
            tr = await _transcript(ticker, latest["year"], latest["quarter"])
            out["transcript"] = {
                "success": tr.get("success"),
                "paragraphs": len(tr.get("paragraphs", [])),
                "char_count": tr.get("char_count"),
                "truncated": tr.get("truncated"),
            }
    except Exception as e:
        out["transcripts"] = {"success": False, "error": str(e)}
    for st in ("income", "balance", "cash_flow"):
        try:
            f = await _financials(ticker, st, "annual", 3)
            out[f"financials:{st}"] = {"success": f.get("success"), "rows": len(f.get("rows", []))}
        except Exception as e:
            out[f"financials:{st}"] = {"success": False, "error": str(e)}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        rest = [a for a in sys.argv[1:] if a != "--probe"]
        asyncio.run(_probe(rest))
    else:
        log.info(
            "defeatbeta MCP starting on http://%s:%s/mcp (cache=%s)",
            mcp.settings.host,
            mcp.settings.port,
            _CACHE_DIR,
        )
        from _mcp_auth import serve  # audit I1: opt-in MCP_SHARED_SECRET bearer gate
        serve(mcp)
