"""B7 — Pool operations + observability (25 pts)."""
from __future__ import annotations

import asyncio

import httpx

from app.alerts import evaluate_alerts
from app.state import Proxy, state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer, MockReceiver


async def test_b7_1_metrics_shape(no_lifespan_client: httpx.AsyncClient) -> None:
    body = (await no_lifespan_client.get("/metrics")).json()
    required = {"total_checks", "current_pool_size", "active_alerts", "total_alerts", "webhook_deliveries"}
    assert required <= set(body)
    for k in required:
        assert isinstance(body[k], int), f"{k} not int: {body[k]!r}"


async def test_b7_2_total_checks_matches_per_proxy_sum(
    client: httpx.AsyncClient, proxy_server: MockProxyServer
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    for i in range(3):
        proxy_server.set(f"px-{i}", status=200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(3)], "replace": True},
    )
    await asyncio.sleep(2.5)
    metrics = (await client.get("/metrics")).json()
    per_proxy_sum = sum(p.total_checks for p in state.proxies.values())
    assert metrics["total_checks"] == per_proxy_sum


async def test_b7_3_pool_size_matches(no_lifespan_client: httpx.AsyncClient) -> None:
    await no_lifespan_client.post(
        "/proxies",
        json={"proxies": [f"https://x/p/p{i}" for i in range(7)], "replace": True},
    )
    body = (await no_lifespan_client.get("/metrics")).json()
    assert body["current_pool_size"] == 7


async def test_b7_4_active_alerts_zero_or_one(no_lifespan_client: httpx.AsyncClient) -> None:
    state.proxies.clear()
    for i in range(5):
        state.proxies[f"px-{i}"] = Proxy(id=f"px-{i}", url="x", status="up")
    state.proxies["px-0"].status = "down"
    state.proxies["px-1"].status = "down"
    await evaluate_alerts()
    body = (await no_lifespan_client.get("/metrics")).json()
    assert body["active_alerts"] in (0, 1)
    assert body["active_alerts"] == 1


async def test_b7_5_total_alerts_matches_endpoint(no_lifespan_client: httpx.AsyncClient) -> None:
    state.proxies.clear()
    for i in range(5):
        state.proxies[f"px-{i}"] = Proxy(id=f"px-{i}", url="x", status="up")
    state.proxies["px-0"].status = "down"
    state.proxies["px-1"].status = "down"
    await evaluate_alerts()
    state.proxies["px-0"].status = "up"
    state.proxies["px-1"].status = "up"
    await evaluate_alerts()
    state.proxies["px-0"].status = "down"
    state.proxies["px-1"].status = "down"
    await evaluate_alerts()
    metrics = (await no_lifespan_client.get("/metrics")).json()
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert metrics["total_alerts"] == len(alerts) == 2


async def test_b7_6_webhook_deliveries_increment(
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
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    metrics = (await client.get("/metrics")).json()
    assert metrics["webhook_deliveries"] >= 1


async def test_b7_7_delete_clears_pool_keeps_alerts(no_lifespan_client: httpx.AsyncClient) -> None:
    state.proxies.clear()
    for i in range(5):
        state.proxies[f"px-{i}"] = Proxy(id=f"px-{i}", url="x", status="up")
    state.proxies["px-0"].status = "down"
    state.proxies["px-1"].status = "down"
    await evaluate_alerts()
    # Persist alerts count
    before = len((await no_lifespan_client.get("/alerts")).json())
    await no_lifespan_client.delete("/proxies")
    metrics = (await no_lifespan_client.get("/metrics")).json()
    assert metrics["current_pool_size"] == 0
    assert metrics["total_alerts"] == before


async def test_b7_8_delete_during_active_resolves_and_delivers(
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
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    await client.delete("/proxies")
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.resolved")) >= 1, timeout=5)
    assert ok
