"""Depth-aware execution: maker first, size vs depth, stop placement, savings. Books and prints are TEST DATA."""
from decimal import Decimal as D

from app.delta_lab.depth_exec import (BookStep, cap_size, entry_cost_bps, liquidity_wall, maker_first_entry, place_stop, round_step, summarise_savings, taker_cost_bps, walk_book)

BIDS = [[D("100.0"), D(50)], [D("99.5"), D(40)], [D("99.0"), D(30)], [D("98.5"), D(20)], [D("98.0"), D(10)]]
ASKS = [[D("100.5"), D(50)], [D("101.0"), D(40)], [D("101.5"), D(30)], [D("102.0"), D(20)], [D("102.5"), D(10)]]


def steps(*specs):
    return [BookStep(t, b, a, prints) for t, b, a, prints in specs]


def test_maker_fills_only_when_the_market_trades_through_the_limit():
    s = steps((0, BIDS, ASKS, ()), (5, BIDS, ASKS, [(D("100.0"), "buy")]),            # a buy print at our bid is not a fill for a long
              (10, BIDS, ASKS, [(D("100.0"), "sell")]))                                # a sell print at the bid is
    out = maker_first_entry("long", D(5), s, lambda t: True)
    assert out.result == "maker_filled" and out.price == D("100.0") and out.reprices == 0


def test_reprices_at_most_n_times_and_only_while_the_signal_is_valid():
    def book(px):
        return [[D(px), D(50)]], [[D(px) + D("0.5"), D(50)]]
    s = [BookStep(i * 3, *book(100 + i), ()) for i in range(8)]                          # the bid keeps moving up
    out = maker_first_entry("long", D(1), s, lambda t: True, max_reprices=3, window_s=100, max_taker_cost_bps=D(0))
    assert out.reprices == 3 and out.result == "missed"                                  # capped; taker cost above the 0 bps limit -> skipped
    gone = maker_first_entry("long", D(1), s, lambda t: t < 7, max_reprices=3, window_s=100)
    assert gone.result == "missed" and "signal no longer valid" in gone.reason


def test_taker_fallback_only_when_signal_valid_and_cost_under_the_limit_else_missed_fill_is_logged():
    s = steps((0, BIDS, ASKS, ()), (31, BIDS, ASKS, ()))                                 # window (30 s) ends unfilled
    ok = maker_first_entry("long", D(5), s, lambda t: True, window_s=30, max_taker_cost_bps=D(30))
    assert ok.result == "taker_filled" and ok.price == D("100.5") and ok.expected_taker_cost_bps is not None
    no = maker_first_entry("long", D(5), s, lambda t: True, window_s=30, max_taker_cost_bps=D("1"))
    assert no.result == "missed" and "missed fill" in no.reason and no.expected_taker_cost_bps > D(1)
    thin = maker_first_entry("long", D(500), s, lambda t: True, window_s=30)
    assert thin.result == "missed" and "too thin" in " ".join(thin.log) + thin.reason


def test_size_is_capped_at_ten_percent_of_top5_depth_and_the_cut_is_logged():
    d = cap_size("long", BIDS, ASKS, 30)                                                  # asks top 5 = 150 -> 10% = 15
    assert d.allowed == 15 and d.cut and "size cut 30 -> 15" in d.note
    assert cap_size("long", BIDS, ASKS, 10).cut is False
    assert cap_size("short", BIDS, ASKS, 100).allowed == 15                              # bids top 5 = 150
    assert cap_size("long", [], [], 5).allowed == 0                                      # unknown book allows nothing
    assert cap_size("long", BIDS, ASKS, 100, within_bps=D(50)).allowed >= 1


def test_stop_goes_past_the_extreme_by_an_atr_buffer_and_off_round_numbers():
    tick = D("0.5")
    p = place_stop("long", D("1000.0"), D(8), tick)                                      # base 1000 - 2 = 998 ; near round 1000? 998 is within 20 ticks (10) of 1000 on the 100 grid
    assert p.stop < D("1000.0") and p.base_stop == D("998.0") and p.distance_from_extreme > 0
    assert p.stop != D("1000") and any("round number" in n for n in p.moved_for)
    far = place_stop("short", D("1234.0"), D(10), tick)
    assert far.stop > D("1234.0") and far.moved_for == [] and far.distance_from_extreme == D("2.5")


def test_stop_is_placed_past_a_visible_liquidity_wall_not_just_beyond_it():
    tick = D("0.5")
    bids = [[D("1000") - D("0.5") * i, D(10)] for i in range(1, 20)]
    bids[6] = [bids[6][0], D(400)]                                                       # a wall of 400 against a median of 10
    wall = liquidity_wall(bids)
    assert wall == bids[6][0]
    p = place_stop("long", wall + D("1"), D(2), tick, book_beyond=bids, buffer_atr=D("0.5"))   # base lands right on the wall
    assert p.stop < wall and p.wall_price == wall and any("liquidity wall" in n for n in p.moved_for)
    assert round_step(D("59000")) == D(500) and round_step(D("5")) == D("0.5")


def test_cost_comparison_and_savings_summary():
    maker = entry_cost_bps("maker", "long", BIDS, ASKS, D(5))
    taker = entry_cost_bps("taker", "long", BIDS, ASKS, D(5))
    assert maker == D("0.0002") * 10000 * D("1.18") and taker > maker                    # taker pays the larger fee plus half-spread and impact
    assert taker_cost_bps("long", BIDS, ASKS, D(5)) > 0 and walk_book(ASKS, D(60)) == (D("100.5") * 50 + D("101.0") * 10) / 60
    events = [{"saving_bps": D(x)} for x in (2, 4, 6, 8, 10)] + [{"missed": True, "missed_net_bps": D(-5)}, {"missed": True, "missed_net_bps": D(15)}]
    r = summarise_savings(events)
    assert r["median_saving_bps"] == 6 and r["p90_saving_bps"] == 10 and abs(r["missed_fill_rate"] - 2 / 7) < 1e-9 and r["missed_trades_mean_net_bps"] == 5
