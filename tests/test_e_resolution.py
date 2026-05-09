"""B5 — Alert resolution (20 pts)."""
from __future__ import annotations

import asyncio
import re

import httpx

from app.alerts import evaluate_alerts
from app.state import Proxy, state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer, MockReceiver


ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


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


async def test_b5_1_recovery_transitions_to_resolved(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    await _set_down(["px-000", "px-001", "px-002"])
    await _set_down([])
    a = (await no_lifespan_client.get("/alerts")).json()[0]
    assert a["status"] == "resolved"


async def test_b5_2_resolved_at_iso_z(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    await _set_down(["px-000", "px-001", "px-002"])
    await _set_down([])
    a = (await no_lifespan_client.get("/alerts")).json()[0]
    assert ISO_Z.match(a["resolved_at"]), a["resolved_at"]


async def test_b5_3_resolved_webhook_delivered(
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
    # Heal them
    proxy_server.set("px-0", status=200)
    proxy_server.set("px-1", status=200)
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.resolved")) >= 1, timeout=5)
    assert ok


async def test_b5_4_resolved_payload_shape(
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
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    await wait_for(lambda: len(receiver.successful_for_event("alert.resolved")) >= 1, timeout=5)
    body = receiver.successful_for_event("alert.resolved")[0]["body"]
    assert body["event"] == "alert.resolved"
    assert "alert_id" in body
    assert ISO_Z.match(body["resolved_at"])


async def test_b5_5_resolved_alert_keeps_original_fields(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    await _set_down(["px-000", "px-001", "px-002"])
    fired = (await no_lifespan_client.get("/alerts")).json()[0]
    fired_id = fired["alert_id"]
    fired_at = fired["fired_at"]
    await _set_down([])
    a = (await no_lifespan_client.get("/alerts")).json()[0]
    assert a["alert_id"] == fired_id
    assert a["fired_at"] == fired_at
    assert a["resolved_at"] is not None


async def test_b5_6_no_active_after_resolve(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed(10)
    await _set_down(["px-000", "px-001", "px-002"])
    await _set_down([])
    assert state.current_active_alert_id is None
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert all(a["status"] != "active" for a in alerts)


async def test_b5_7_resolution_exactly_once(
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
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    await wait_for(lambda: len(receiver.successful_for_event("alert.resolved")) >= 1, timeout=5)
    await asyncio.sleep(3.0)
    assert len(receiver.successful_for_event("alert.resolved")) == 1
