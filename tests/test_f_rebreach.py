"""B6 — Re-breach lifecycle (30 pts)."""
from __future__ import annotations

import asyncio

import httpx

from app.alerts import evaluate_alerts
from app.state import Proxy, state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer, MockReceiver


def _seed(n: int) -> None:
    state.proxies.clear()
    for i in range(n):
        pid = f"px-{i:03d}"
        state.proxies[pid] = Proxy(id=pid, url="x", status="up")


async def _set_down(ids: list[str]) -> None:
    for pid in state.proxies:
        state.proxies[pid].status = "up"
    for pid in ids:
        state.proxies[pid].status = "down"
    await evaluate_alerts()


async def test_b6_1_rebreach_mints_new_id(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    await _set_down(["px-000", "px-001"])
    first = (await no_lifespan_client.get("/alerts")).json()[0]["alert_id"]
    await _set_down([])
    await _set_down(["px-000", "px-001"])
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert alerts[1]["alert_id"] != first


async def test_b6_2_old_resolved_alert_unchanged(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    await _set_down(["px-000", "px-001"])
    await _set_down([])
    resolved = (await no_lifespan_client.get("/alerts")).json()[0]
    old_id = resolved["alert_id"]
    old_resolved_at = resolved["resolved_at"]
    await _set_down(["px-000", "px-001"])
    found = next(
        a for a in (await no_lifespan_client.get("/alerts")).json()
        if a["alert_id"] == old_id
    )
    assert found["status"] == "resolved"
    assert found["resolved_at"] == old_resolved_at


async def test_b6_3_alerts_returns_both(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    await _set_down(["px-000", "px-001"])
    await _set_down([])
    await _set_down(["px-000", "px-001"])
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert len(alerts) == 2
    assert {a["status"] for a in alerts} == {"resolved", "active"}


async def test_b6_4_event_order_on_receiver(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    # cycle 1: fire alert1
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    # cycle 2: heal -> resolve alert1
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    await wait_for(lambda: len(receiver.successful_for_event("alert.resolved")) >= 1, timeout=5)
    # cycle 3: re-breach -> fire alert2
    for i in range(2):
        proxy_server.set(f"px-{i}", status=503)
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 2, timeout=5)
    # cycle 4: heal again -> resolve alert2
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    await wait_for(lambda: len(receiver.successful_for_event("alert.resolved")) >= 2, timeout=5)

    seq = [
        (e["body"]["event"], e["body"]["alert_id"])
        for e in receiver.successful()
    ]
    fires = [s for s in seq if s[0] == "alert.fired"]
    resolves = [s for s in seq if s[0] == "alert.resolved"]
    assert len(fires) == 2
    assert len(resolves) == 2
    a1 = fires[0][1]
    a2 = fires[1][1]
    assert a1 != a2
    # Per-receiver order: alert.fired(a1), alert.resolved(a1), alert.fired(a2), alert.resolved(a2)
    expected = [
        ("alert.fired", a1), ("alert.resolved", a1),
        ("alert.fired", a2), ("alert.resolved", a2),
    ]
    assert seq == expected, f"got {seq}"


async def test_b6_5_no_interleaving(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    # Same setup as b6_4 — covered by exact sequence equality. Add explicit check:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    await wait_for(lambda: len(receiver.successful_for_event("alert.resolved")) >= 1, timeout=5)
    for i in range(2):
        proxy_server.set(f"px-{i}", status=503)
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 2, timeout=5)
    seq = [(e["body"]["event"], e["body"]["alert_id"]) for e in receiver.successful()]
    a1 = seq[0][1]
    a1_resolved_idx = next(i for i, s in enumerate(seq) if s == ("alert.resolved", a1))
    a2_fired_idx = next(i for i, s in enumerate(seq) if s[0] == "alert.fired" and s[1] != a1)
    assert a1_resolved_idx < a2_fired_idx, "alert.resolved(old) must precede alert.fired(new)"


async def test_b6_6_five_cycles_distinct_ids(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    ids = []
    for _ in range(5):
        await _set_down(["px-000", "px-001"])
        ids.append(state.current_active_alert_id)
        await _set_down([])
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert len(alerts) == 5
    assert len({a["alert_id"] for a in alerts}) == 5
