# Binary Outcome Order-Matching Engine

A REST API implementing a limit-order book for a binary prediction market (YES / NO outcomes).

---

## What I Built

### Domain model

A binary outcome market lets participants buy one of two mutually exclusive outcomes: **YES** or **NO**. Each contract is worth exactly \$1 if the outcome it represents is correct. A YES order at price `p` says "I'll pay `p` for a YES contract"; a NO order at price `q` says "I'll pay `q` for a NO contract". Two orders **cross** (i.e., can be matched) when `p_yes + p_no >= 1.0`—together they're willing to pay at least \$1 for complementary contracts.

### Engine

`app/engine.py` — the core matching logic.

| Feature | Implementation |
|---|---|
| **Price-time priority** | Within each price level, orders are stored in a `deque` (FIFO). Best (highest) price across levels is matched first. |
| **Partial fills** | Both maker and taker track `filled` / `remaining` independently. A partially-filled maker stays on the book at its original price level. |
| **Self-trade prevention** | An incoming taker skips any maker with the same `user_id`. It skips the _entire_ price level (not just one order) to avoid leaving a confusing half-filled level that the same user can never take. Orders skipped due to STP are not removed from the book. |
| **Cancel-while-matching race** | Every market holds a `threading.Lock`. `submit_order` and `cancel_order` both acquire it, so a cancel and a match for the same market are strictly serialised — a cancel can never partially interleave with a match. |
| **Order book snapshot** | Aggregates remaining quantity per price level, returned in descending price order (best bid first for each side). |
| **Recent trades** | Stored in a `deque(maxlen=50)` per market; returned in the snapshot. |

### API

`app/main.py` — FastAPI layer.

| Method | Path | Description |
|---|---|---|
| `POST` | `/orders` | Submit a new limit order |
| `DELETE` | `/orders/{market_id}/{order_id}` | Cancel a resting order |
| `GET` | `/orders/{market_id}/{order_id}` | Fetch current state of an order |
| `GET` | `/orderbook/{market_id}` | Order book snapshot + recent trades |
| `GET` | `/health` | Health check |

Markets are created implicitly on first order submission.

### Edge cases explicitly handled

1. **Self-trades** — same `user_id` on both sides; skipped, both orders remain.
2. **Partial fills** — taker or maker can be partially filled; partial state tracked on the `Order` object.
3. **Cancel-while-matching** — per-market lock prevents this race entirely.
4. **Stale orders on the book** — defensive check for `is_active()` before filling even inside the lock.
5. **Price level cleanup** — exhausted price levels are deleted from the dict to keep the book compact.
6. **Taker not added to book if fully filled** — taker is only added to the resting book if it has remaining quantity after matching.
7. **Boundary prices** — `price` must be strictly in `(0, 1)` exclusive; validated by Pydantic.
8. **Floating-point cross check** — rounded to 10 decimal places to avoid representation drift at the `0.5 + 0.5 == 1.0` boundary.
9. **Trade history cap** — capped at 50 per market to bound memory.
10. **Market isolation** — each market is completely independent; a crash or race in one does not affect others.

---

## How to Run

```bash
pip install -r requirements.txt

# Run the API server
uvicorn app.main:app --reload

# Interactive docs
open http://localhost:8000/docs

# Run tests
pytest tests/ -v
```

---

## Example Session

```bash
# Post a YES order at 0.55
curl -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"market_id":"BTCUSD","user_id":"alice","side":"YES","price":0.55,"quantity":10}'

# Post a crossing NO order — will immediately match
curl -X POST http://localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -d '{"market_id":"BTCUSD","user_id":"bob","side":"NO","price":0.55,"quantity":5}'

# Check the book
curl http://localhost:8000/orderbook/BTCUSD
```

---

## What I'd Do With More Time

### Correctness & robustness
- **Persistent storage** (PostgreSQL with `SELECT ... FOR UPDATE`) to survive restarts; the in-memory design loses all state on crash.
- **Idempotency keys** on order submission so duplicate HTTP requests don't create duplicate orders.
- **Decimal arithmetic** instead of `float` to eliminate all floating-point rounding issues (Python's `decimal.Decimal` or a fixed-point integer representation).
- **Formal order lifecycle state machine** with explicit transitions and guards to prevent impossible state changes.

### Correctness at scale
- **Persistent message queue** (Kafka / SQS) per market to serialize order events before they hit the engine, replacing the threading lock with a single-consumer model — simpler to reason about and horizontally scalable.
- **Optimistic locking / versioning** on orders for multi-process deployments.
- **Event sourcing** — record every order event; the order book is a read projection. Enables full audit trail and replay.

### Features
- **Market-order support** (price = "market") — fill at whatever the best available price is.
- **Order expiry / GTD** — good-till-date orders that auto-cancel at a timestamp.
- **WebSocket feed** — push trade events and book updates to subscribers instead of polling.
- **Position accounting** — track per-user exposure; reject orders that would exceed position limits.
- **Fee model** — maker/taker fee schedule applied at fill time.
- **Market resolution** — settle all open positions when an outcome is decided.
- **Rate limiting** — prevent order-spam attacks.

### Operations
- **Metrics** (Prometheus/Grafana) — order throughput, match latency p99, book depth.
- **Structured logging** with correlation IDs through the order lifecycle.
- **Integration tests** against a real DB (not just in-memory fixtures).
- **Load / chaos tests** — simulate cancel storms, market-order floods, STP-heavy workloads.

