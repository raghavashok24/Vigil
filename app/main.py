"""FastAPI application for the binary-outcome order-matching engine."""

from fastapi import FastAPI, HTTPException, status

from .engine import MatchingEngine
from .models import (
    CancelOrderResponse,
    Order,
    OrderBookSnapshot,
    SubmitOrderRequest,
    SubmitOrderResponse,
)

app = FastAPI(
    title="Binary Outcome Order-Matching Engine",
    description=(
        "A limit-order book for binary prediction markets.  "
        "YES orders buy the YES outcome; NO orders buy the NO outcome.  "
        "Orders cross when their combined prices ≥ 1.0."
    ),
    version="1.0.0",
)

engine = MatchingEngine()


@app.post(
    "/orders",
    response_model=SubmitOrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a new limit order",
)
def submit_order(req: SubmitOrderRequest) -> SubmitOrderResponse:
    """
    Submit a limit order to the matching engine.

    - **market_id**: identifier for the market (created on first order)
    - **user_id**: identifier for the submitting user
    - **side**: `YES` or `NO`
    - **price**: probability price, strictly between 0 and 1
    - **quantity**: number of contracts (positive integer)

    Returns the created order plus any trades that were immediately generated.
    """
    order = Order(
        market_id=req.market_id,
        user_id=req.user_id,
        side=req.side,
        price=req.price,
        quantity=req.quantity,
    )
    order, trades = engine.submit_order(order)
    return SubmitOrderResponse(order=order, trades=trades)


@app.delete(
    "/orders/{market_id}/{order_id}",
    response_model=CancelOrderResponse,
    summary="Cancel a resting order",
)
def cancel_order(market_id: str, order_id: str) -> CancelOrderResponse:
    """
    Cancel a resting (OPEN or PARTIAL) order.

    Returns 404 if the order does not exist or is already filled/cancelled.
    """
    order = engine.cancel_order(market_id, order_id)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_id} not found or not cancellable in market {market_id}",
        )
    return CancelOrderResponse(order=order)


@app.get(
    "/orders/{market_id}/{order_id}",
    response_model=Order,
    summary="Get a single order",
)
def get_order(market_id: str, order_id: str) -> Order:
    """Retrieve the current state of a single order."""
    order = engine.get_order(market_id, order_id)
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Order {order_id} not found in market {market_id}",
        )
    return order


@app.get(
    "/orderbook/{market_id}",
    response_model=OrderBookSnapshot,
    summary="Get order book snapshot",
)
def get_order_book(market_id: str) -> OrderBookSnapshot:
    """
    Return a point-in-time snapshot of the order book for a market.

    - **yes_bids**: resting YES orders, best (highest) price first
    - **no_bids**: resting NO orders, best (highest) price first
    - **trades**: last 50 trades in this market
    """
    snapshot = engine.get_snapshot(market_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Market {market_id} not found",
        )
    return snapshot


@app.get("/health", summary="Health check")
def health() -> dict:
    return {"status": "ok"}
