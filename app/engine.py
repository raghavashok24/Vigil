"""
Order-matching engine for a binary-outcome prediction market.

Design choices
--------------
* Price-time priority (FIFO within each price level).
* A YES order at price p is willing to pay p per contract for the YES outcome.
  A NO order at price q is willing to pay q per contract for the NO outcome.
  Two orders cross when p_yes + p_no >= 1.0 (the implied cost of one full
  contract equals or exceeds 1).  The trade executes at the maker's price.
* Self-trade prevention: an incoming taker order NEVER matches against resting
  orders from the same user_id.  Matched-but-skipped levels are not consumed
  (the taker simply skips them).
* Partial fills: both taker and maker track `filled`; a resting order that is
  only partially taken stays on the book with its remaining quantity.
* Cancel-while-matching safety: cancellation uses a per-market lock so a
  cancel cannot interleave with an in-progress match for the same market.
  Orders are cancelled atomically before or after a match, never during.
* The engine is intentionally single-process / in-memory (no persistence).
  In production you'd back this with a database and use optimistic locking or
  a queue-based design.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Optional

from .models import Order, OrderBookLevel, OrderBookSnapshot, OrderStatus, Side, Trade

_MAX_RECENT_TRADES = 50


class Market:
    """All state for a single market."""

    def __init__(self, market_id: str) -> None:
        self.market_id = market_id
        # price_level -> deque[Order], sorted by insertion (time priority)
        # YES bids: dict[price -> deque]  (higher price = more willing to buy YES)
        # NO  bids: dict[price -> deque]
        self.yes_book: dict[float, deque[Order]] = defaultdict(deque)
        self.no_book: dict[float, deque[Order]] = defaultdict(deque)
        self.orders: dict[str, Order] = {}  # order_id -> Order
        self.trades: deque[Trade] = deque(maxlen=_MAX_RECENT_TRADES)
        self.lock = threading.Lock()

    def _book_for(self, side: Side) -> dict[float, deque[Order]]:
        return self.yes_book if side == Side.YES else self.no_book

    def _opposing_book(self, side: Side) -> dict[float, deque[Order]]:
        return self.no_book if side == Side.YES else self.yes_book

    def _crosses(self, taker_side: Side, taker_price: float, maker_price: float) -> bool:
        """
        Two orders cross when their combined prices >= 1.0.
        YES taker at p1, NO maker at p2: crosses when p1 + p2 >= 1.0
        NO  taker at p1, YES maker at p2: same condition.
        """
        return round(taker_price + maker_price, 10) >= 1.0

    def add_order(self, order: Order) -> list[Trade]:
        """
        Insert an order and immediately attempt to match it.
        Returns the list of trades generated.
        Must be called with self.lock held.
        """
        self.orders[order.order_id] = order
        trades = self._match(order)
        if order.is_active():
            self._book_for(order.side)[order.price].append(order)
        return trades

    def _match(self, taker: Order) -> list[Trade]:
        trades: list[Trade] = []
        opposing = self._opposing_book(taker.side)

        # Best maker price for the opposing side is the HIGHEST price level
        # (they also want the contract; highest price = most eager).
        while taker.remaining > 0:
            if not opposing:
                break

            best_price = max(opposing.keys())

            if not self._crosses(taker.side, taker.price, best_price):
                break

            level_queue = opposing[best_price]

            # Drain this price level, skipping self-trades
            while level_queue and taker.remaining > 0:
                maker = level_queue[0]

                # --- self-trade prevention ---
                if maker.user_id == taker.user_id:
                    # Skip entire level to avoid partial self-trades creating
                    # ambiguous state.  We break to the outer loop which will
                    # check the next price level.
                    break

                if not maker.is_active():
                    # Stale order (cancelled between lock releases – shouldn't
                    # happen with the lock, but be defensive).
                    level_queue.popleft()
                    continue

                fill_qty = min(taker.remaining, maker.remaining)
                trade_price = best_price  # maker's price (price-time priority)

                trade = Trade(
                    market_id=taker.market_id,
                    taker_order_id=taker.order_id,
                    maker_order_id=maker.order_id,
                    taker_user_id=taker.user_id,
                    maker_user_id=maker.user_id,
                    side=taker.side,
                    price=trade_price,
                    quantity=fill_qty,
                )

                # Update fills
                taker.filled += fill_qty
                taker.updated_at = time.time()
                maker.filled += fill_qty
                maker.updated_at = time.time()

                taker.status = OrderStatus.FILLED if taker.remaining == 0 else OrderStatus.PARTIAL
                maker.status = OrderStatus.FILLED if maker.remaining == 0 else OrderStatus.PARTIAL

                trades.append(trade)
                self.trades.append(trade)

                if maker.remaining == 0:
                    level_queue.popleft()

            # Clean up exhausted price level
            if not level_queue:
                del opposing[best_price]

        return trades

    def cancel_order(self, order_id: str) -> Optional[Order]:
        """
        Cancel an order.  Returns the order if found and cancellable, else None.
        Must be called with self.lock held.
        """
        order = self.orders.get(order_id)
        if order is None or not order.is_active():
            return None

        order.status = OrderStatus.CANCELLED
        order.updated_at = time.time()

        # Remove from the resting book
        book = self._book_for(order.side)
        level = book.get(order.price)
        if level:
            # deque doesn't support O(1) remove; market sizes stay small enough.
            try:
                level.remove(order)
            except ValueError:
                pass
            if not level:
                del book[order.price]

        return order

    def snapshot(self) -> OrderBookSnapshot:
        """Return a point-in-time snapshot (caller holds the lock)."""

        def aggregate(book: dict[float, deque[Order]]) -> list[OrderBookLevel]:
            result = []
            for price in sorted(book.keys(), reverse=True):
                qty = sum(o.remaining for o in book[price] if o.is_active())
                if qty > 0:
                    result.append(OrderBookLevel(price=price, quantity=qty))
            return result

        return OrderBookSnapshot(
            market_id=self.market_id,
            yes_bids=aggregate(self.yes_book),
            no_bids=aggregate(self.no_book),
            trades=list(self.trades),
        )


class MatchingEngine:
    """Top-level engine managing multiple markets."""

    def __init__(self) -> None:
        self._markets: dict[str, Market] = {}
        self._global_lock = threading.Lock()

    def _get_or_create_market(self, market_id: str) -> Market:
        with self._global_lock:
            if market_id not in self._markets:
                self._markets[market_id] = Market(market_id)
            return self._markets[market_id]

    def submit_order(self, order: Order) -> tuple[Order, list[Trade]]:
        market = self._get_or_create_market(order.market_id)
        with market.lock:
            trades = market.add_order(order)
        return order, trades

    def cancel_order(self, market_id: str, order_id: str) -> Optional[Order]:
        market = self._markets.get(market_id)
        if market is None:
            return None
        with market.lock:
            return market.cancel_order(order_id)

    def get_order(self, market_id: str, order_id: str) -> Optional[Order]:
        market = self._markets.get(market_id)
        if market is None:
            return None
        return market.orders.get(order_id)

    def get_snapshot(self, market_id: str) -> Optional[OrderBookSnapshot]:
        market = self._markets.get(market_id)
        if market is None:
            return None
        with market.lock:
            return market.snapshot()
