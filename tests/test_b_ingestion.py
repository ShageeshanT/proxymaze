"""B2 — Pool ingestion + background monitoring (45 pts)."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.state import Alert, state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer


URL = "https://example.com/proxy/{pid}"


async def test_b2_1_post_proxies_returns_201_with_pending(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.post(
        "/proxies",
        json={"proxies": [URL.format(pid=f"px-{i}") for i in range(3)], "replace": True},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["accepted"] == 3
    assert all(p["status"] == "pending" for p in body["proxies"])


async def test_b2_2_id_is_last_path_segment(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.post(
        "/proxies",
        json={"proxies": ["https://example.com/proxy/px-101"], "replace": True},
    )
    assert r.json()["proxies"][0]["id"] == "px-101"


async def test_b2_3_append_grows_pool(no_lifespan_client: httpx.AsyncClient) -> None:
    await no_lifespan_client.post(
        "/proxies",
        json={"proxies": [URL.format(pid="a"), URL.format(pid="b")], "replace": True},
    )
    await no_lifespan_client.post("/proxies", json={"proxies": [URL.format(pid="c")]})
    body = (await no_lifespan_client.get("/proxies")).json()
    assert body["total"] == 3
    assert {p["id"] for p in body["proxies"]} == {"a", "b", "c"}


async def test_b2_4_replace_resets_pool(no_lifespan_client: httpx.AsyncClient) -> None:
    await no_lifespan_client.post(
        "/proxies",
        json={"proxies": [URL.format(pid=p) for p in "abc"], "replace": True},
    )
    await no_lifespan_client.post(
        "/proxies",
        json={"proxies": [URL.format(pid="z")], "replace": True},
    )
    body = (await no_lifespan_client.get("/proxies")).json()
    assert body["total"] == 1
    assert body["proxies"][0]["id"] == "z"


async def test_b2_5_replace_preserves_alerts(no_lifespan_client: httpx.AsyncClient) -> None:
    state.alerts.append(Alert(
        alert_id="alert-keep", fired_at="t", resolved_at="t2",
        failure_rate_at_fire=0.5, total_at_fire=2, failed_at_fire=1, failed_ids_at_fire=["a"],
        failure_rate_at_resolve=0.0, total_at_resolve=2, failed_at_resolve=0, failed_ids_at_resolve=[],
    ))
    await no_lifespan_client.post(
        "/proxies", json={"proxies": [URL.format(pid="x")], "replace": True}
    )
    alerts = (await no_lifespan_client.get("/alerts")).json()
    assert any(a["alert_id"] == "alert-keep" for a in alerts)


async def test_b2_6_extra_fields_accepted(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.post(
        "/proxies",
        json={"proxies": [URL.format(pid="x")], "tags": ["foo"], "label": 1},
    )
    assert r.status_code == 201


async def test_b2_7_malformed_json_is_4xx(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.post(
        "/proxies", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert 400 <= r.status_code < 500


async def test_b2_8_proxies_summary_shape(no_lifespan_client: httpx.AsyncClient) -> None:
    await no_lifespan_client.post(
        "/proxies",
        json={"proxies": [URL.format(pid="x")], "replace": True},
    )
    body = (await no_lifespan_client.get("/proxies")).json()
    assert {"total", "up", "down", "failure_rate", "proxies"} <= set(body)
    p = body["proxies"][0]
    assert {"id", "url", "status", "last_checked_at", "consecutive_failures"} <= set(p)


async def test_b2_9_failure_rate_calc(no_lifespan_client: httpx.AsyncClient) -> None:
    from app.state import Proxy
    state.proxies.clear()
    for i in range(7):
        state.proxies[f"u{i}"] = Proxy(id=f"u{i}", url="x", status="up")
    for i in range(3):
        state.proxies[f"d{i}"] = Proxy(id=f"d{i}", url="x", status="down")
    body = (await no_lifespan_client.get("/proxies")).json()
    assert body["total"] == 10
    assert body["up"] == 7
    assert body["down"] == 3
    assert body["failure_rate"] == pytest.approx(0.3, abs=0.01)


async def test_b2_10_background_loop_transitions_pending_to_up(
    client: httpx.AsyncClient, proxy_server: MockProxyServer
) -> None:
    # Load proxies, do NOT call /proxies again — only the background loop
    # should transition statuses.
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post(
        "/proxies",
        json={
            "proxies": [proxy_server.url_for(f"px-{i}") for i in range(4)],
            "replace": True,
        },
    )
    # Wait for at least one cycle to land.
    ok = await wait_for(
        lambda: all(p.status == "up" for p in state.proxies.values()),
        timeout=5.0,
    )
    assert ok, f"statuses: {[(p.id, p.status) for p in state.proxies.values()]}"


async def test_b2_11_parallel_probing(
    client: httpx.AsyncClient, proxy_server: MockProxyServer
) -> None:
    # 50 proxies each delaying ~0.6s. Sequential would be 30s; parallel is ~0.6s.
    # Use a 1s interval so the prober picks up the new pool promptly after POST.
    n = 50
    for i in range(n):
        proxy_server.set(f"px-{i}", status=200, delay_ms=600)
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 2000})
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(n)], "replace": True},
    )
    start = time.monotonic()
    ok = await wait_for(
        lambda: all(p.status == "up" for p in state.proxies.values()),
        timeout=5.0,
    )
    elapsed = time.monotonic() - start
    assert ok, f"only {sum(1 for p in state.proxies.values() if p.status=='up')}/{n} up"
    # If sequential, this would take ~30s (50 × 0.6s). 5s is a safe parallel bound.
    assert elapsed < 5.0, f"cycle took {elapsed:.2f}s — looks sequential"


async def test_b2_12_delete_returns_204_preserves_alerts(no_lifespan_client: httpx.AsyncClient) -> None:
    state.alerts.append(Alert(
        alert_id="alert-keep", fired_at="t", resolved_at="t2",
        failure_rate_at_fire=0.5, total_at_fire=1, failed_at_fire=1, failed_ids_at_fire=["a"],
        failure_rate_at_resolve=0.0, total_at_resolve=1, failed_at_resolve=0, failed_ids_at_resolve=[],
    ))
    await no_lifespan_client.post("/proxies", json={"proxies": [URL.format(pid="a")]})
    r = await no_lifespan_client.delete("/proxies")
    assert r.status_code == 204
    assert (await no_lifespan_client.get("/proxies")).json()["total"] == 0
    assert len((await no_lifespan_client.get("/alerts")).json()) == 1


async def test_b2_13_history_is_array(no_lifespan_client: httpx.AsyncClient) -> None:
    await no_lifespan_client.post(
        "/proxies", json={"proxies": [URL.format(pid="x")], "replace": True}
    )
    r = await no_lifespan_client.get("/proxies/x/history")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_b2_14_unknown_proxy_404(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.get("/proxies/never-existed")
    assert r.status_code == 404


async def test_b2_15_unknown_history_404(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.get("/proxies/never/history")
    assert r.status_code == 404
