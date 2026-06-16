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
