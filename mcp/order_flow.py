"""Order-flow accumulation for the tradier stream consumer (Phase 2 / Tier2).

Pure logic only — NO aiohttp/redis/mcp imports — so it's unit-testable in
isolation (test_order_flow.py). tradier_mcp.py imports from here and wires
classify + accumulation into _publish_event, exposing it via get_order_flow.

Lee-Ready trade classification on Tradier streaming `timesale` events, which
carry bid/ask/last/size (confirmed live 2026-06-16). Quote rule → midpoint →
tick-rule fallback. Accumulates signed dollar volume per symbol, resets at the
start of each ET trading day.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def classify_timesale(
    last: float | None,
    bid: float | None,
    ask: float | None,
    prev_last: float | None,
) -> str | None:
    """'buy' | 'sell' | None. Lee-Ready: quote rule, then midpoint, then tick rule."""
    if last is None or last <= 0:
        return None
    if bid is not None and ask is not None and ask > bid:
        if last >= ask:
            return "buy"
        if last <= bid:
            return "sell"
        mid = (bid + ask) / 2
        if last > mid:
            return "buy"
        if last < mid:
            return "sell"
        # exactly at midpoint → tick rule
    # no usable quote, or exactly at midpoint → tick rule (compare to previous trade)
    if prev_last is not None:
        if last > prev_last:
            return "buy"
        if last < prev_last:
            return "sell"
    return None


def et_date_from_ms(ms: int) -> str:
    """Epoch milliseconds → 'YYYY-MM-DD' in US/Eastern."""
    return datetime.datetime.fromtimestamp(ms / 1000, tz=_ET).strftime("%Y-%m-%d")


@dataclass
class _Row:
    buy_dollars: float = 0.0
    sell_dollars: float = 0.0
    buy_ct: int = 0
    sell_ct: int = 0
    unclassified_ct: int = 0
    prev_last: float | None = None
    session_date: str | None = None


class OrderFlowState:
    """Per-symbol signed-dollar accumulator with start-of-ET-day reset.

    In-memory only; lives for the lifetime of the MCP process. record() is
    called from _publish_event for each timesale event; snapshot() is read by
    the get_order_flow tool (and stream_status). Never raises on bad input —
    bad fields just yield an unclassified tick.
    """

    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}

    def record(
        self,
        symbol: str,
        last: float | None,
        bid: float | None,
        ask: float | None,
        size: float | None,
        date_ms: int | None,
    ) -> None:
        if not symbol:
            return
        row = self.rows.get(symbol)
        day = et_date_from_ms(date_ms) if date_ms else (row.session_date if row else None)
        if row is None or (day is not None and day != row.session_date):
            row = _Row(session_date=day)  # fresh day (or first event) → reset
            self.rows[symbol] = row
        side = classify_timesale(last, bid, ask, row.prev_last)
        dollar = (last or 0.0) * (size or 0.0)
        if side == "buy":
            row.buy_dollars += dollar
            row.buy_ct += 1
        elif side == "sell":
            row.sell_dollars += dollar
            row.sell_ct += 1
        else:
            row.unclassified_ct += 1
        if last is not None and last > 0:
            row.prev_last = last

    def snapshot(self, symbols: list[str] | None = None) -> dict[str, dict]:
        keys = symbols if symbols is not None else list(self.rows.keys())
        out: dict[str, dict] = {}
        for sym in keys:
            row = self.rows.get(sym)
            if row is None:
                continue
            classified = row.buy_ct + row.sell_ct
            total = classified + row.unclassified_ct
            denom = row.buy_dollars + row.sell_dollars
            out[sym] = {
                "buy_dollars": round(row.buy_dollars, 2),
                "sell_dollars": round(row.sell_dollars, 2),
                "ofi": (row.buy_dollars - row.sell_dollars) / denom if denom > 0 else None,
                "buy_ct": row.buy_ct,
                "sell_ct": row.sell_ct,
                "classified_ct": classified,
                "unclassified_ct": row.unclassified_ct,
                "coverage": round(classified / total, 4) if total > 0 else 0.0,
                "session_date": row.session_date,
            }
        return out
