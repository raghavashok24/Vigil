"""Integration tests for the REST API layer."""

import pytest
from fastapi.testclient import TestClient

from app.main import app, engine
from app.engine import MatchingEngine


@pytest.fixture(autouse=True)
def fresh_engine(monkeypatch):
    """Replace the global engine with a fresh one for each test."""
    import app.main as main_module
    new_engine = MatchingEngine()
    monkeypatch.setattr(main_module, "engine", new_engine)
    yield new_engine


@pytest.fixture
def client():
    return TestClient(app)


# ── Submit order ───────────────────────────────────────────────────────────

class TestSubmitOrder:
    def test_submit_yes_order(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.4, "quantity": 10
        })
        assert r.status_code == 201
        body = r.json()
        assert body["order"]["status"] == "OPEN"
        assert body["trades"] == []

    def test_submit_no_order(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "B",
            "side": "NO", "price": 0.4, "quantity": 5
        })
        assert r.status_code == 201

    def test_price_validation_zero(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.0, "quantity": 5
        })
        assert r.status_code == 422

    def test_price_validation_one(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 1.0, "quantity": 5
        })
        assert r.status_code == 422

    def test_quantity_validation_zero(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.5, "quantity": 0
        })
        assert r.status_code == 422

    def test_quantity_validation_negative(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.5, "quantity": -1
        })
        assert r.status_code == 422

    def test_immediate_match_returns_trades(self, client):
        client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.55, "quantity": 5
        })
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "B",
            "side": "NO", "price": 0.55, "quantity": 5
        })
        assert r.status_code == 201
        body = r.json()
        assert len(body["trades"]) == 1
        assert body["order"]["status"] == "FILLED"


# ── Cancel order ───────────────────────────────────────────────────────────

class TestCancelOrder:
    def test_cancel_open_order(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.4, "quantity": 10
        })
        oid = r.json()["order"]["order_id"]
        r2 = client.delete(f"/orders/M/{oid}")
        assert r2.status_code == 200
        assert r2.json()["order"]["status"] == "CANCELLED"

    def test_cancel_unknown_order(self, client):
        # Need to create the market first
        client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.4, "quantity": 1
        })
        r = client.delete("/orders/M/nonexistent-id")
        assert r.status_code == 404

    def test_cancel_unknown_market(self, client):
        r = client.delete("/orders/GHOST/nonexistent-id")
        assert r.status_code == 404

    def test_cancel_already_filled(self, client):
        client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.55, "quantity": 5
        })
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "B",
            "side": "NO", "price": 0.55, "quantity": 5
        })
        maker_id = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.55, "quantity": 5
        }).json()["order"]["order_id"]
        # fill it
        client.post("/orders", json={
            "market_id": "M", "user_id": "B",
            "side": "NO", "price": 0.55, "quantity": 5
        })
        r = client.delete(f"/orders/M/{maker_id}")
        assert r.status_code == 404


# ── Get order ─────────────────────────────────────────────────────────────

class TestGetOrder:
    def test_get_existing_order(self, client):
        r = client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.4, "quantity": 10
        })
        oid = r.json()["order"]["order_id"]
        r2 = client.get(f"/orders/M/{oid}")
        assert r2.status_code == 200
        assert r2.json()["order_id"] == oid

    def test_get_unknown_order(self, client):
        client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.4, "quantity": 1
        })
        r = client.get("/orders/M/ghost")
        assert r.status_code == 404

    def test_get_order_unknown_market(self, client):
        r = client.get("/orders/GHOST/any-id")
        assert r.status_code == 404


# ── Order book ────────────────────────────────────────────────────────────

class TestOrderBook:
    def test_snapshot_structure(self, client):
        client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.4, "quantity": 5
        })
        r = client.get("/orderbook/M")
        assert r.status_code == 200
        body = r.json()
        assert "yes_bids" in body
        assert "no_bids" in body
        assert "trades" in body
        assert body["yes_bids"][0]["quantity"] == 5

    def test_snapshot_unknown_market(self, client):
        r = client.get("/orderbook/UNKNOWN")
        assert r.status_code == 404

    def test_snapshot_after_full_match(self, client):
        client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.55, "quantity": 5
        })
        client.post("/orders", json={
            "market_id": "M", "user_id": "B",
            "side": "NO", "price": 0.55, "quantity": 5
        })
        r = client.get("/orderbook/M")
        body = r.json()
        assert body["yes_bids"] == []
        assert body["no_bids"] == []
        assert len(body["trades"]) == 1

    def test_snapshot_shows_partial_remainder(self, client):
        client.post("/orders", json={
            "market_id": "M", "user_id": "A",
            "side": "YES", "price": 0.55, "quantity": 10
        })
        client.post("/orders", json={
            "market_id": "M", "user_id": "B",
            "side": "NO", "price": 0.55, "quantity": 3
        })
        r = client.get("/orderbook/M")
        body = r.json()
        assert body["yes_bids"][0]["quantity"] == 7


# ── Health check ──────────────────────────────────────────────────────────

class TestHealth:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
