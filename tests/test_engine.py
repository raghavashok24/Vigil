"""Unit tests for the matching engine (no HTTP layer)."""

import threading
import time

import pytest

from app.engine import MatchingEngine
from app.models import Order, OrderStatus, Side


def make_engine() -> MatchingEngine:
    return MatchingEngine()


def yes(engine, price, qty, user="A", market="M"):
    o = Order(market_id=market, user_id=user, side=Side.YES, price=price, quantity=qty)
    return engine.submit_order(o)


def no(engine, price, qty, user="B", market="M"):
    o = Order(market_id=market, user_id=user, side=Side.NO, price=price, quantity=qty)
    return engine.submit_order(o)


# ── Basic matching ─────────────────────────────────────────────────────────

class TestBasicMatching:
    def test_no_cross_below_threshold(self):
        e = make_engine()
        yes(e, 0.4, 10)
        no(e, 0.5, 10)  # 0.4 + 0.5 < 1.0 — no cross
        snap = e.get_snapshot("M")
        assert len(snap.yes_bids) == 1
        assert len(snap.no_bids) == 1
        assert snap.trades == []

    def test_exact_cross_at_1(self):
        e = make_engine()
        yes(e, 0.5, 5)
        _, trades = no(e, 0.5, 5)  # 0.5 + 0.5 == 1.0
        assert len(trades) == 1
        assert trades[0].quantity == 5

    def test_cross_above_1(self):
        e = make_engine()
        yes(e, 0.6, 5)
        _, trades = no(e, 0.6, 5)  # 0.6 + 0.6 > 1.0
        assert len(trades) == 1
        assert trades[0].quantity == 5

    def test_taker_fully_filled(self):
        e = make_engine()
        yes(e, 0.55, 100)
        order, trades = no(e, 0.55, 10)
        assert order.status == OrderStatus.FILLED
        assert sum(t.quantity for t in trades) == 10

    def test_maker_fully_filled(self):
        e = make_engine()
        yes(e, 0.55, 10)
        order, trades = no(e, 0.55, 100)
        assert sum(t.quantity for t in trades) == 10
        snap = e.get_snapshot("M")
        assert snap.yes_bids == []  # maker exhausted

    def test_trade_price_is_maker_price(self):
        """Taker gets the maker's price (price-time priority)."""
        e = make_engine()
        yes(e, 0.55, 10)               # maker
        _, trades = no(e, 0.60, 10)    # taker willing to pay more
        assert trades[0].price == 0.55

    def test_multiple_price_levels_best_first(self):
        """Best price matched first."""
        e = make_engine()
        yes(e, 0.50, 5, user="A")
        yes(e, 0.55, 5, user="A")
        yes(e, 0.60, 5, user="A")
        _, trades = no(e, 0.45, 12, user="B")
        # All three levels cross (0.45+0.55, 0.45+0.60, etc.)
        prices = [t.price for t in trades]
        assert prices == sorted(prices, reverse=True)  # best maker first


# ── Partial fills ──────────────────────────────────────────────────────────

class TestPartialFills:
    def test_partial_taker(self):
        e = make_engine()
        yes(e, 0.55, 3)
        order, trades = no(e, 0.55, 10)
        assert order.status == OrderStatus.PARTIAL
        assert order.filled == 3
        assert order.remaining == 7

    def test_partial_maker_stays_on_book(self):
        e = make_engine()
        order_yes, _ = yes(e, 0.55, 20)
        no(e, 0.55, 7)
        assert order_yes.status == OrderStatus.PARTIAL
        assert order_yes.filled == 7
        snap = e.get_snapshot("M")
        assert snap.yes_bids[0].quantity == 13

    def test_taker_sweeps_multiple_makers(self):
        e = make_engine()
        yes(e, 0.55, 5, user="A")
        yes(e, 0.55, 5, user="C")
        _, trades = no(e, 0.55, 8, user="B")
        assert sum(t.quantity for t in trades) == 8
        snap = e.get_snapshot("M")
        remaining_yes = sum(l.quantity for l in snap.yes_bids)
        assert remaining_yes == 2


# ── Self-trade prevention ─────────────────────────────────────────────────

class TestSelfTradePrevention:
    def test_self_trade_not_executed(self):
        e = make_engine()
        yes(e, 0.55, 10, user="SAME")
        _, trades = no(e, 0.55, 10, user="SAME")
        assert trades == []

    def test_self_trade_skips_to_next_user(self):
        e = make_engine()
        yes(e, 0.55, 10, user="SAME")
        yes(e, 0.55, 5, user="OTHER")
        _, trades = no(e, 0.55, 5, user="SAME")
        assert len(trades) == 1
        assert trades[0].maker_user_id == "OTHER"

    def test_self_trade_order_remains_on_book(self):
        """Self-traded order must not be consumed."""
        e = make_engine()
        yes(e, 0.55, 10, user="SAME")
        no(e, 0.55, 10, user="SAME")
        snap = e.get_snapshot("M")
        # Both orders resting; YES unfilled, NO unfilled
        assert snap.yes_bids[0].quantity == 10
        assert snap.no_bids[0].quantity == 10

    def test_partial_fill_around_self_trade(self):
        """Fill part from another user, skip self-trade level."""
        e = make_engine()
        yes(e, 0.60, 5, user="OTHER")   # best price, different user
        yes(e, 0.55, 5, user="SAME")    # next level, same user
        _, trades = no(e, 0.55, 10, user="SAME")
        assert len(trades) == 1
        assert trades[0].quantity == 5
        assert trades[0].maker_user_id == "OTHER"


# ── Cancellation ──────────────────────────────────────────────────────────

class TestCancellation:
    def test_cancel_open_order(self):
        e = make_engine()
        order, _ = yes(e, 0.4, 10)
        cancelled = e.cancel_order("M", order.order_id)
        assert cancelled.status == OrderStatus.CANCELLED
        snap = e.get_snapshot("M")
        assert snap.yes_bids == []

    def test_cancel_nonexistent_returns_none(self):
        e = make_engine()
        e._get_or_create_market("M")
        assert e.cancel_order("M", "ghost-id") is None

    def test_cancel_filled_returns_none(self):
        e = make_engine()
        order, _ = yes(e, 0.55, 5)
        no(e, 0.55, 5)
        assert order.status == OrderStatus.FILLED
        assert e.cancel_order("M", order.order_id) is None

    def test_cancel_partial_order(self):
        e = make_engine()
        order, _ = yes(e, 0.55, 10)
        no(e, 0.55, 3)
        assert order.status == OrderStatus.PARTIAL
        cancelled = e.cancel_order("M", order.order_id)
        assert cancelled.status == OrderStatus.CANCELLED
        snap = e.get_snapshot("M")
        assert snap.yes_bids == []

    def test_cancel_unknown_market_returns_none(self):
        e = make_engine()
        assert e.cancel_order("GHOST_MARKET", "any-id") is None


# ── Order book snapshot ───────────────────────────────────────────────────

class TestOrderBookSnapshot:
    def test_snapshot_aggregates_levels(self):
        e = make_engine()
        yes(e, 0.40, 3, user="A")
        yes(e, 0.40, 7, user="C")
        yes(e, 0.30, 5, user="D")
        snap = e.get_snapshot("M")
        assert snap.yes_bids[0].price == 0.40
        assert snap.yes_bids[0].quantity == 10
        assert snap.yes_bids[1].price == 0.30
        assert snap.yes_bids[1].quantity == 5

    def test_snapshot_descending_price_order(self):
        e = make_engine()
        for p in [0.3, 0.5, 0.4]:
            yes(e, p, 1, user="A")
        snap = e.get_snapshot("M")
        prices = [l.price for l in snap.yes_bids]
        assert prices == sorted(prices, reverse=True)

    def test_snapshot_excludes_cancelled(self):
        e = make_engine()
        order, _ = yes(e, 0.4, 10)
        e.cancel_order("M", order.order_id)
        snap = e.get_snapshot("M")
        assert snap.yes_bids == []

    def test_snapshot_unknown_market_returns_none(self):
        e = make_engine()
        assert e.get_snapshot("NOPE") is None

    def test_recent_trades_in_snapshot(self):
        e = make_engine()
        yes(e, 0.55, 5)
        no(e, 0.55, 5)
        snap = e.get_snapshot("M")
        assert len(snap.trades) == 1

    def test_trades_capped_at_50(self):
        e = make_engine()
        for i in range(60):
            yes(e, 0.55, 1, user="A", market="M")
            no(e, 0.55, 1, user="B", market="M")
        snap = e.get_snapshot("M")
        assert len(snap.trades) <= 50


# ── Edge cases ────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_zero_remaining_after_partial_not_on_book(self):
        e = make_engine()
        yes(e, 0.55, 5)
        no(e, 0.55, 5)
        snap = e.get_snapshot("M")
        assert snap.yes_bids == []

    def test_multiple_markets_isolated(self):
        e = make_engine()
        yes(e, 0.55, 10, market="M1")
        no(e, 0.55, 10, market="M2")
        snap1 = e.get_snapshot("M1")
        snap2 = e.get_snapshot("M2")
        assert snap1.yes_bids[0].quantity == 10
        assert snap2.trades == []

    def test_price_time_priority_within_level(self):
        """Earlier order at same price filled before later one."""
        e = make_engine()
        o1, _ = yes(e, 0.55, 5, user="FIRST")
        o2, _ = yes(e, 0.55, 5, user="SECOND")
        no(e, 0.55, 3, user="TAKER")
        assert o1.filled == 3
        assert o2.filled == 0

    def test_taker_not_added_to_book_when_fully_filled(self):
        e = make_engine()
        yes(e, 0.55, 10)
        order, _ = no(e, 0.55, 5)
        assert order.status == OrderStatus.FILLED
        snap = e.get_snapshot("M")
        assert snap.no_bids == []

    def test_large_quantity_sweep(self):
        e = make_engine()
        for _ in range(100):
            yes(e, 0.55, 1, user="A", market="M")
        _, trades = no(e, 0.55, 100, user="B", market="M")
        total = sum(t.quantity for t in trades)
        assert total == 100

    def test_order_status_transitions(self):
        e = make_engine()
        o, _ = yes(e, 0.55, 10)
        assert o.status == OrderStatus.OPEN
        no(e, 0.55, 5)
        assert o.status == OrderStatus.PARTIAL
        no(e, 0.55, 5)
        assert o.status == OrderStatus.FILLED

    def test_cross_at_boundary_0_99(self):
        e = make_engine()
        yes(e, 0.99, 1)
        _, trades = no(e, 0.01, 1)
        assert len(trades) == 1

    def test_no_cross_just_below_1(self):
        e = make_engine()
        yes(e, 0.49, 1)
        _, trades = no(e, 0.50, 1)  # 0.49 + 0.50 = 0.99 < 1.0
        assert trades == []


# ── Concurrency smoke test ─────────────────────────────────────────────────

class TestConcurrency:
    def test_concurrent_submissions_no_crash(self):
        e = make_engine()
        errors = []

        def submit_yes(i):
            try:
                yes(e, 0.55, 1, user=f"U{i}", market="CM")
            except Exception as exc:
                errors.append(exc)

        def submit_no(i):
            try:
                no(e, 0.55, 1, user=f"V{i}", market="CM")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit_yes, args=(i,)) for i in range(50)]
        threads += [threading.Thread(target=submit_no, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrency errors: {errors}"
        snap = e.get_snapshot("CM")
        # All 100 YES + 100 NO orders, each 1 unit → 50 trades, 50 each side consumed
        total_filled = sum(t.quantity for t in snap.trades)
        assert total_filled > 0

    def test_concurrent_cancel_and_match(self):
        """Cancel racing against a match should not double-decrement fills."""
        e = make_engine()
        order, _ = yes(e, 0.55, 100, user="MAKER", market="RC")
        errors = []

        def do_cancel():
            try:
                e.cancel_order("RC", order.order_id)
            except Exception as exc:
                errors.append(exc)

        def do_match():
            try:
                no(e, 0.55, 10, user="TAKER", market="RC")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_cancel)]
        threads += [threading.Thread(target=do_match) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # filled + remaining == original quantity
        assert order.filled + order.remaining == 100
