from order_flow import classify_timesale

def test_at_or_above_ask_is_buy():
    assert classify_timesale(269.91, 269.37, 269.91, None) == "buy"
    assert classify_timesale(270.00, 269.37, 269.91, None) == "buy"

def test_at_or_below_bid_is_sell():
    assert classify_timesale(269.37, 269.37, 269.91, None) == "sell"
    assert classify_timesale(269.00, 269.37, 269.91, None) == "sell"

def test_inside_spread_uses_midpoint():
    # mid = 269.64; above → buy, below → sell
    assert classify_timesale(269.70, 269.37, 269.91, None) == "buy"
    assert classify_timesale(269.50, 269.37, 269.91, None) == "sell"

def test_at_midpoint_falls_back_to_tick():
    mid = 269.64  # = (269.37+269.91)/2
    assert classify_timesale(mid, 269.37, 269.91, 269.60) == "buy"   # tick up
    assert classify_timesale(mid, 269.37, 269.91, 269.70) == "sell"  # tick down
    assert classify_timesale(mid, 269.37, 269.91, mid) is None       # zero tick, no call

def test_no_quote_uses_tick_rule():
    assert classify_timesale(100.0, None, None, 99.5) == "buy"
    assert classify_timesale(100.0, None, None, 100.5) == "sell"
    assert classify_timesale(100.0, None, None, 100.0) is None
    assert classify_timesale(100.0, None, None, None) is None

def test_crossed_or_locked_book_uses_tick():
    # ask <= bid (bad book) → tick rule
    assert classify_timesale(100.0, 101.0, 100.5, 99.0) == "buy"

def test_bad_last_is_none():
    assert classify_timesale(0.0, 1, 2, 1.5) is None
    assert classify_timesale(None, 1, 2, 1.5) is None

from order_flow import OrderFlowState, et_date_from_ms

def test_et_date_from_ms():
    # 2026-06-16T17:15:00Z ≈ 13:15 ET → "2026-06-16"
    assert et_date_from_ms(1781630100000) == "2026-06-16"

def test_accumulates_signed_dollars():
    st = OrderFlowState()
    d = 1781630100000  # same ET day
    st.record("SPY", last=101.0, bid=100.0, ask=101.0, size=10, date_ms=d)  # >=ask buy, $1010
    st.record("SPY", last=100.0, bid=100.0, ask=101.0, size=5,  date_ms=d)  # <=bid sell, $500
    snap = st.snapshot(["SPY"])["SPY"]
    assert snap["buy_dollars"] == 1010.0
    assert snap["sell_dollars"] == 500.0
    assert snap["buy_ct"] == 1 and snap["sell_ct"] == 1
    assert snap["ofi"] == (1010.0 - 500.0) / (1010.0 + 500.0)
    assert snap["coverage"] == 1.0

def test_unclassified_counted_not_in_dollars():
    st = OrderFlowState()
    d = 1781630100000
    # no quote, no prev_last → unclassified
    st.record("QQQ", last=50.0, bid=None, ask=None, size=10, date_ms=d)
    snap = st.snapshot(["QQQ"])["QQQ"]
    assert snap["buy_ct"] == 0 and snap["sell_ct"] == 0
    assert snap["unclassified_ct"] == 1
    assert snap["ofi"] is None          # nothing classified
    assert snap["coverage"] == 0.0

def test_daily_reset_on_et_date_change():
    st = OrderFlowState()
    day1 = 1781630100000           # 2026-06-16
    day2 = day1 + 24 * 3600 * 1000 # next day
    st.record("SPY", 101.0, 100.0, 101.0, 10, day1)  # buy $1010 on day1
    st.record("SPY", 101.0, 100.0, 101.0, 7,  day2)  # new day → reset, then buy $707
    snap = st.snapshot(["SPY"])["SPY"]
    assert snap["buy_dollars"] == 707.0   # day1 wiped
    assert snap["session_date"] == "2026-06-17"

def test_snapshot_missing_symbol_absent():
    st = OrderFlowState()
    assert st.snapshot(["NOPE"]) == {}
