"""B9 — Discord integration (+10 pts)."""
from __future__ import annotations

import httpx

from app.state import state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer, MockReceiver


REQUIRED_NAMES = ["alert id", "failure rate", "failed proxies", "threshold", "failed ids"]


async def _setup_breach(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/integrations", json={
        "type": "discord", "webhook_url": receiver.url_for("discord"),
    })
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )


async def test_b9_1_post_integrations_discord(
    no_lifespan_client: httpx.AsyncClient, receiver: MockReceiver
) -> None:
    r = await no_lifespan_client.post("/integrations", json={
        "type": "discord", "webhook_url": receiver.url_for("discord"),
    })
    assert r.status_code in (200, 201)


async def test_b9_2_discord_webhook_received(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/discord" for e in receiver.received), timeout=5)
    assert ok
    e = next(e for e in receiver.received if e["path"] == "/discord")
    assert e["headers"].get("content-type", "").startswith("application/json")


async def test_b9_3_title_present(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    await wait_for(lambda: any(e["path"] == "/discord" for e in receiver.received), timeout=5)
    body = next(e for e in receiver.received if e["path"] == "/discord")["body"]
    title = body["embeds"][0].get("title")
    assert isinstance(title, str) and title


async def test_b9_4_description_present(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    await wait_for(lambda: any(e["path"] == "/discord" for e in receiver.received), timeout=5)
    body = next(e for e in receiver.received if e["path"] == "/discord")["body"]
    desc = body["embeds"][0].get("description")
    assert isinstance(desc, str) and desc


async def test_b9_5_color_int_in_range(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    await wait_for(lambda: any(e["path"] == "/discord" for e in receiver.received), timeout=5)
    body = next(e for e in receiver.received if e["path"] == "/discord")["body"]
    color = body["embeds"][0]["color"]
    assert isinstance(color, int) and not isinstance(color, bool)
    assert 0 <= color <= 16777215


async def test_b9_6_field_names_required(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    await wait_for(lambda: any(e["path"] == "/discord" for e in receiver.received), timeout=5)
    body = next(e for e in receiver.received if e["path"] == "/discord")["body"]
    names = " ".join(f["name"] for f in body["embeds"][0]["fields"]).lower()
    for needle in REQUIRED_NAMES:
        assert needle in names, f"missing required field name substring: {needle!r}"


async def test_b9_7_footer_text(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    await wait_for(lambda: any(e["path"] == "/discord" for e in receiver.received), timeout=5)
    body = next(e for e in receiver.received if e["path"] == "/discord")["body"]
    footer = body["embeds"][0].get("footer", {})
    assert isinstance(footer.get("text"), str) and footer["text"]
