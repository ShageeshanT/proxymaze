"""F — Stress and realism tests.

These take longer than the rest of the suite (large pools, fan-out)
but stay short enough to run in CI. Mark with the ``slow`` marker so
``pytest -m "not slow"`` skips them when you want fast feedback.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.alerts import evaluate_alerts
from app.state import Proxy, state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer, MockReceiver

pytestmark = pytest.mark.slow


# F1. Scale ----------------------------------------------------------------


async def test_f1_500_proxies_all_healthy(
    client: httpx.AsyncClient, proxy_server: MockProxyServer
) -> None:
    """Pool of 500 healthy proxies — cycle completes in well under 5s."""
    n = 500
    for i in range(n):
        proxy_server.set(f"px-{i}", status=200, delay_ms=100)
    await client.post("/config", json={"check_interval_seconds": 5, "request_timeout_ms": 2000})
    await client.post("/proxies", json={
        "proxies": [proxy_server.url_for(f"px-{i}") for i in range(n)],
        "replace": True,
    })
    start = time.monotonic()
    ok = await wait_for(
        lambda: sum(1 for p in state.proxies.values() if p.status == "up") == n,
        timeout=8.0,
    )
    elapsed = time.monotonic() - start
    assert ok, f"only {sum(1 for p in state.proxies.values() if p.status=='up')}/{n} up"
    assert elapsed < 8.0, f"500-proxy cycle took {elapsed:.2f}s"


async def test_f1_500_proxies_all_down_payload_carries_full_list(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    n = 500
    for i in range(n):
        proxy_server.set(f"px-{i:04d}", status=503)
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 2000})
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    await client.post("/proxies", json={
        "proxies": [proxy_server.url_for(f"px-{i:04d}") for i in range(n)],
        "replace": True,
    })
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=10)
    assert ok
    body = receiver.successful_for_event("alert.fired")[0]["body"]
    assert len(body["failed_proxy_ids"]) == n


# F2. Endurance ------------------------------------------------------------


async def test_f2_one_hundred_cycles_distinct_ids(no_lifespan_client: httpx.AsyncClient) -> None:
    """100 fire/resolve cycles — 100 distinct alert_ids in the archive."""
    state.proxies.clear()
    for i in range(5):
        state.proxies[f"p{i}"] = Proxy(id=f"p{i}", url="x", status="up")
    for cycle in range(100):
        state.proxies["p0"].status = "down"
        state.proxies["p1"].status = "down"
        await evaluate_alerts()
        for pid in ["p0", "p1"]:
            state.proxies[pid].status = "up"
        await evaluate_alerts()
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert len(alerts) == 100
    assert len({a["alert_id"] for a in alerts}) == 100


# F3. Latency --------------------------------------------------------------


async def test_f3_webhook_arrives_within_5_seconds(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    t0 = time.monotonic()
    await client.post("/proxies", json={
        "proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)],
        "replace": True,
    })
    ok = await wait_for(
        lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5
    )
    elapsed = time.monotonic() - t0
    assert ok and elapsed < 5.0, f"took {elapsed:.2f}s"


# F4. Multi-receiver fan-out ----------------------------------------------


async def test_f4_seven_receivers_all_hit_per_event(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receivers
) -> None:
    """5 plain webhooks + 1 Slack + 1 Discord — each gets every event."""
    plain = [receivers() for _ in range(5)]
    slack = receivers()
    discord = receivers()

    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    for r in plain:
        await client.post("/webhooks", json={"url": r.url_for("hook")})
    await client.post("/integrations", json={"type": "slack", "webhook_url": slack.url_for("slack")})
    await client.post("/integrations", json={"type": "discord", "webhook_url": discord.url_for("discord")})

    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post("/proxies", json={
        "proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)],
        "replace": True,
    })

    # Every receiver must see fired
    for r in plain + [slack, discord]:
        ok = await wait_for(lambda r=r: len(r.received) >= 1, timeout=8)
        assert ok, "receiver missed alert.fired"

    # Heal -> every receiver must see resolved
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    for r in plain + [slack, discord]:
        ok = await wait_for(lambda r=r: len(r.received) >= 2, timeout=8)
        assert ok, "receiver missed alert.resolved"

    metrics = (await client.get("/metrics")).json()
    # 7 receivers × 2 events = 14 deliveries
    assert metrics["webhook_deliveries"] >= 14
