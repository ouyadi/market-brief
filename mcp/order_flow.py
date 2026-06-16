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
