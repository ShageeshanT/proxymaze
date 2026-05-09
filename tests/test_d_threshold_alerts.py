"""B4 — Threshold breach + webhook delivery (90 pts)."""
from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from app.alerts import evaluate_alerts
from app.prober import probe_pool
from app.state import Proxy, state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer, MockReceiver


ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _seed_pool(n: int) -> None:
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


# ----------------------------------------------------------------- firing


async def test_b4_1_below_threshold_no_fire() -> None:
    _seed_pool(10)
    await _set_down(["px-000"])
    assert state.current_active_alert_id is None


async def test_b4_2_at_threshold_fires() -> None:
    _seed_pool(10)
    await _set_down(["px-000", "px-001"])  # 20%
    assert state.current_active_alert_id is not None


async def test_b4_3_above_threshold_fires() -> None:
    _seed_pool(10)
    await _set_down(["px-000", "px-001", "px-002"])
    assert state.current_active_alert_id is not None


async def test_b4_4_one_in_five_fires() -> None:
    _seed_pool(5)
    await _set_down(["px-000"])
    assert state.current_active_alert_id is not None


async def test_b4_5_one_in_one_fires() -> None:
    _seed_pool(1)
    await _set_down(["px-000"])
    assert state.current_active_alert_id is not None


async def test_b4_6_empty_pool_no_crash() -> None:
    _seed_pool(0)
    await evaluate_alerts()
    assert state.current_active_alert_id is None


# ------------------------------------------------------- alert object shape


async def test_b4_7_alert_shape(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed_pool(10)
    await _set_down(["px-000", "px-001", "px-002"])
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert len(alerts) == 1
    a = alerts[0]
    required = {
        "alert_id", "status", "failure_rate", "total_proxies",
        "failed_proxies", "failed_proxy_ids", "threshold",
        "fired_at", "resolved_at", "message",
    }
    assert required <= set(a)
    assert a["status"] == "active"
    assert a["threshold"] == 0.2
    assert a["resolved_at"] is None
    assert a["alert_id"]


async def test_b4_8_failed_ids_match_currently_down(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed_pool(10)
    down_ids = ["px-002", "px-005", "px-007"]
    await _set_down(down_ids)
    a = (await no_lifespan_client.get("/alerts")).json()[0]
    assert sorted(a["failed_proxy_ids"]) == sorted(down_ids)


async def test_b4_9_fired_at_iso_z(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed_pool(5)
    await _set_down(["px-000", "px-001"])
    a = (await no_lifespan_client.get("/alerts")).json()[0]
    assert ISO_Z.match(a["fired_at"]), a["fired_at"]


async def test_b4_10_alert_id_nonempty(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed_pool(5)
    await _set_down(["px-000"])
    a = (await no_lifespan_client.get("/alerts")).json()[0]
    assert isinstance(a["alert_id"], str) and len(a["alert_id"]) > 0


# ---------------------------------------------------------- webhook delivery


async def test_b4_11_post_webhooks_returns_201(
    no_lifespan_client: httpx.AsyncClient, receiver: MockReceiver
) -> None:
    r = await no_lifespan_client.post("/webhooks", json={"url": receiver.url_for("hook")})
    assert r.status_code == 201
    body = r.json()
    assert "webhook_id" in body
    assert body["url"] == receiver.url_for("hook")


async def test_b4_12_fire_delivers_to_webhook(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    assert ok
    entry = receiver.successful_for_event("alert.fired")[0]
    assert entry["headers"].get("content-type", "").startswith("application/json")


async def test_b4_13_payload_contains_required_fields(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    assert ok
    body = receiver.successful_for_event("alert.fired")[0]["body"]
    required = {
        "event", "alert_id", "fired_at", "failure_rate", "total_proxies",
        "failed_proxies", "failed_proxy_ids", "threshold", "message",
    }
    assert required <= set(body)
    assert body["event"] == "alert.fired"
    assert body["threshold"] == 0.2


async def test_b4_14_alert_id_in_webhook_matches_alerts_endpoint(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    api_id = (await client.get("/alerts")).json()[0]["alert_id"]
    wh_id = receiver.successful_for_event("alert.fired")[0]["body"]["alert_id"]
    assert api_id == wh_id


async def test_b4_15_failed_ids_consistent_across_views(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(10):
        proxy_server.set(f"px-{i}", status=503 if i < 3 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(10)], "replace": True},
    )
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    proxies = (await client.get("/proxies")).json()
    alerts = (await client.get("/alerts")).json()
    pool_down = sorted(p["id"] for p in proxies["proxies"] if p["status"] == "down")
    alert_failed = sorted(alerts[0]["failed_proxy_ids"])
    wh_failed = sorted(receiver.successful_for_event("alert.fired")[0]["body"]["failed_proxy_ids"])
    assert pool_down == alert_failed == wh_failed


# -------------------------------------------------------- multiple receivers


async def test_b4_16_three_receivers_all_get_event(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receivers
) -> None:
    rs = [receivers() for _ in range(3)]
    for r in rs:
        await client.post("/webhooks", json={"url": r.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    for r in rs:
        ok = await wait_for(lambda r=r: len(r.successful_for_event("alert.fired")) >= 1, timeout=5)
        assert ok, f"receiver did not get fired event"


# ------------------------------------------------------ retry on transient


@pytest.mark.parametrize("transient_code", [500, 502, 503, 504])
async def test_b4_17_to_20_retry_on_transient(
    client: httpx.AsyncClient, proxy_server: MockProxyServer,
    receiver: MockReceiver, transient_code: int,
) -> None:
    receiver.script("/hook", transient_code)  # one failure then 200
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    assert ok
    # Exactly one successful delivery despite the transient.
    assert len(receiver.successful_for_event("alert.fired")) == 1


async def test_b4_21_multi_503_then_200(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    receiver.script("/hook", 503, 503, 503)
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    assert ok


async def test_b4_22_4xx_does_not_loop_forever(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    receiver.default_status = 400
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    # Wait for the alert to fire; receiver should be hit at most a few times
    # (no retry on 4xx) and we must continue without hanging.
    await wait_for(lambda: state.current_active_alert_id is not None, timeout=5)
    await asyncio.sleep(1.0)
    # Receiver hit at most a small bounded number of times.
    assert len(receiver.received) <= 5


# ---------------------------------------------------------- exactly-once


async def test_b4_23_persistent_breach_one_fire(
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
    # Let several cycles run.
    await asyncio.sleep(3.5)
    fires = receiver.successful_for_event("alert.fired")
    assert len(fires) == 1, f"expected 1 fire, got {len(fires)}"


async def test_b4_24_long_breach_with_retries_one_fire(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    receiver.script("/hook", 503, 200)
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    await asyncio.sleep(5.5)
    fires = receiver.successful_for_event("alert.fired")
    assert len(fires) == 1, f"expected 1, got {len(fires)} (received={[r['responded_with'] for r in receiver.received]})"


async def test_b4_25_503_503_then_200_one_success(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    receiver.script("/hook", 503, 503)
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    ok = await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    assert ok
    assert len(receiver.successful_for_event("alert.fired")) == 1


# -------------------------------------------------------- single active


async def test_b4_26_single_active_alert(no_lifespan_client: httpx.AsyncClient) -> None:
    _seed_pool(10)
    await _set_down(["px-000", "px-001", "px-002"])
    await _set_down(["px-000", "px-001", "px-002", "px-003"])
    alerts = (await no_lifespan_client.get("/alerts")).json()
    actives = [a for a in alerts if a["status"] == "active"]
    assert len(actives) == 1


async def test_b4_27_more_failures_update_failed_ids_no_refire(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(10):
        proxy_server.set(f"px-{i}", status=503 if i < 3 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(10)], "replace": True},
    )
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    # Add another down
    proxy_server.set("px-3", status=503)
    await asyncio.sleep(2.5)
    fires = receiver.successful_for_event("alert.fired")
    assert len(fires) == 1
    # /alerts reflects 4 failed
    a = (await client.get("/alerts")).json()[0]
    assert a["failed_proxies"] == 4
    assert "px-3" in a["failed_proxy_ids"]


async def test_b4_28_partial_recovery_still_breach_no_refire(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(10):
        proxy_server.set(f"px-{i}", status=503 if i < 4 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(10)], "replace": True},
    )
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    # Bring one back; still 30% down -> still active, no re-fire
    proxy_server.set("px-3", status=200)
    await asyncio.sleep(2.5)
    assert len(receiver.successful_for_event("alert.fired")) == 1
    a = (await client.get("/alerts")).json()[0]
    assert a["status"] == "active"
    assert a["failed_proxies"] == 3


# ---------------------------------------------------------- consistency


async def test_b4_29_pool_alert_webhook_agree(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/webhooks", json={"url": receiver.url_for("hook")})
    for i in range(10):
        proxy_server.set(f"px-{i}", status=503 if i < 3 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(10)], "replace": True},
    )
    await wait_for(lambda: len(receiver.successful_for_event("alert.fired")) >= 1, timeout=5)
    proxies = (await client.get("/proxies")).json()
    alerts = (await client.get("/alerts")).json()
    pool_down = sorted(p["id"] for p in proxies["proxies"] if p["status"] == "down")
    pool_failure_rate = proxies["failure_rate"]
    pool_total = proxies["total"]
    a = alerts[0]
    assert sorted(a["failed_proxy_ids"]) == pool_down
    assert a["failed_proxies"] == proxies["down"]
    assert a["total_proxies"] == pool_total
    assert abs(a["failure_rate"] - pool_failure_rate) < 0.001
    wh = receiver.successful_for_event("alert.fired")[0]["body"]
    assert sorted(wh["failed_proxy_ids"]) == pool_down
