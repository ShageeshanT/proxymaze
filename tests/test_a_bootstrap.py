"""B1 — Service bootstrap + config (10 pts)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.state import state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer


async def test_b1_1_health_returns_ok(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_b1_2_config_defaults(no_lifespan_client: httpx.AsyncClient) -> None:
    # Reset the autouse fast-config override so we can observe true defaults.
    from app.state import Config
    state.config = Config()
    body = (await no_lifespan_client.get("/config")).json()
    assert body["check_interval_seconds"] == 15
    assert body["request_timeout_ms"] == 3000


async def test_b1_3_post_config_returns_echo(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.post(
        "/config", json={"check_interval_seconds": 5, "request_timeout_ms": 1000}
    )
    assert r.status_code == 200
    assert r.json() == {"check_interval_seconds": 5, "request_timeout_ms": 1000}


async def test_b1_4_get_after_post_reflects_new_values(no_lifespan_client: httpx.AsyncClient) -> None:
    await no_lifespan_client.post(
        "/config", json={"check_interval_seconds": 7, "request_timeout_ms": 1500}
    )
    body = (await no_lifespan_client.get("/config")).json()
    assert body == {"check_interval_seconds": 7, "request_timeout_ms": 1500}


async def test_b1_5_unknown_fields_ignored(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.post(
        "/config",
        json={"check_interval_seconds": 4, "request_timeout_ms": 900, "foo": "bar", "n": 1},
    )
    assert r.status_code == 200
    assert r.json() == {"check_interval_seconds": 4, "request_timeout_ms": 900}


async def test_b1_6_empty_body_does_not_500(no_lifespan_client: httpx.AsyncClient) -> None:
    r = await no_lifespan_client.post("/config", json={})
    # Either accepted (defaults) or 4xx — must not be 500.
    assert r.status_code < 500


async def test_b1_7_hot_reload_takes_effect_within_3_seconds(
    client: httpx.AsyncClient, proxy_server: MockProxyServer
) -> None:
    # Start with a slow interval; load 1 proxy.
    await client.post("/config", json={"check_interval_seconds": 30, "request_timeout_ms": 800})
    proxy_server.set("px-1", status=200)
    await client.post(
        "/proxies", json={"proxies": [proxy_server.url_for("px-1")], "replace": True}
    )
    # Wait briefly so the prober is mid-sleep on the slow interval.
    await asyncio.sleep(0.6)
    initial_calls = proxy_server.calls_per_id.get("px-1", 0)
    # Hot-reload to a fast interval and assert a new probe lands within ~3s.
    await client.post("/config", json={"check_interval_seconds": 2, "request_timeout_ms": 800})
    ok = await wait_for(
        lambda: proxy_server.calls_per_id.get("px-1", 0) > initial_calls,
        timeout=3.0,
    )
    assert ok, (
        f"prober did not pick up new check_interval_seconds within 3s "
        f"(initial={initial_calls}, now={proxy_server.calls_per_id.get('px-1', 0)})"
    )
