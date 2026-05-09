"""Spec tests 4, 5, 6, 8 — alert lifecycle invariants."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.alerts import evaluate_alerts
from app.main import app
from app.state import Proxy, state


def _seed_pool(n: int) -> None:
    state.proxies.clear()
    for i in range(n):
        pid = f"px-{i:03d}"
        state.proxies[pid] = Proxy(id=pid, url=f"https://x.test/proxy/{pid}", status="up")


async def _set_down(ids: list[str]) -> None:
    for pid in state.proxies:
        state.proxies[pid].status = "up"
    for pid in ids:
        state.proxies[pid].status = "down"
    await evaluate_alerts()


async def test_proxies_endpoint_reflects_current_statuses() -> None:
    _seed_pool(5)
    state.proxies["px-002"].status = "down"
    state.proxies["px-002"].consecutive_failures = 1
    body = TestClient(app).get("/proxies").json()
    assert body["total"] == 5
    assert body["up"] == 4
    assert body["down"] == 1
    assert body["failure_rate"] == 0.2
    by_id = {p["id"]: p for p in body["proxies"]}
    assert by_id["px-002"]["status"] == "down"
    assert by_id["px-000"]["status"] == "up"


async def test_persistent_breach_fires_exactly_once() -> None:
    _seed_pool(10)
    await _set_down(["px-000", "px-001", "px-002"])  # 30%
    await _set_down(["px-000", "px-001", "px-002"])  # still 30%, same alert
    await _set_down(["px-000", "px-001", "px-002", "px-003"])  # 40%, still same alert

    alerts = TestClient(app).get("/alerts").json()
    assert len(alerts) == 1
    assert alerts[0]["status"] == "active"


async def test_recovery_then_new_breach_mints_new_id() -> None:
    _seed_pool(10)
    await _set_down(["px-000", "px-001", "px-002"])  # fire
    first_id = TestClient(app).get("/alerts").json()[0]["alert_id"]

    await _set_down([])  # resolve
    alerts = TestClient(app).get("/alerts").json()
    assert len(alerts) == 1
    assert alerts[0]["status"] == "resolved"
    assert alerts[0]["resolved_at"] is not None

    await _set_down(["px-000", "px-001", "px-002"])  # new breach
    alerts = TestClient(app).get("/alerts").json()
    assert len(alerts) == 2
    assert alerts[0]["alert_id"] == first_id
    assert alerts[0]["status"] == "resolved"
    assert alerts[1]["status"] == "active"
    assert alerts[1]["alert_id"] != first_id


async def test_proxies_and_alerts_agree_on_failed_ids() -> None:
    _seed_pool(10)
    await _set_down(["px-003", "px-004", "px-005"])

    c = TestClient(app)
    proxies = c.get("/proxies").json()
    alerts = c.get("/alerts").json()
    active = [a for a in alerts if a["status"] == "active"][0]

    pool_failed_ids = sorted(p["id"] for p in proxies["proxies"] if p["status"] == "down")
    pool_failed = proxies["down"]
    pool_total = proxies["total"]

    assert active["failed_proxy_ids"] == pool_failed_ids
    assert active["failed_proxies"] == pool_failed
    assert active["total_proxies"] == pool_total
    assert active["threshold"] == 0.2

    # Active alert must use LIVE values, not a snapshot — flip another proxy
    # down and re-evaluate; the active alert should reflect it on read.
    await _set_down(["px-003", "px-004", "px-005", "px-006"])
    proxies2 = c.get("/proxies").json()
    alerts2 = c.get("/alerts").json()
    active2 = [a for a in alerts2 if a["status"] == "active"][0]
    assert active2["failed_proxy_ids"] == sorted(p["id"] for p in proxies2["proxies"] if p["status"] == "down")
    assert active2["failed_proxies"] == proxies2["down"]
