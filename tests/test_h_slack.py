"""B8 — Slack integration (+10 pts).

Strict shape check on the live JSON the Slack webhook URL receives.
"""
from __future__ import annotations

import asyncio
import re

import httpx

from app.state import Proxy, state
from tests.conftest import wait_for
from tests.helpers.mock_servers import MockProxyServer, MockReceiver


HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
REQUIRED_TITLES = ["alert id", "failure rate", "failed proxies", "threshold", "failed ids", "fired at"]


async def _setup_breach(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver,
    integ_type: str = "slack", events: list[str] | None = None,
) -> None:
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    body = {"type": integ_type, "webhook_url": receiver.url_for("slack")}
    if events is not None:
        body["events"] = events
    await client.post("/integrations", json=body)
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )


async def test_b8_1_post_integrations_returns_2xx(
    no_lifespan_client: httpx.AsyncClient, receiver: MockReceiver
) -> None:
    r = await no_lifespan_client.post("/integrations", json={
        "type": "slack", "webhook_url": receiver.url_for("slack"),
        "username": "ProxyWatch", "events": ["alert.fired", "alert.resolved"],
    })
    assert r.status_code in (200, 201)


async def test_b8_2_slack_webhook_receives_post(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    e = next(e for e in receiver.received if e["path"] == "/slack")
    assert e["headers"].get("content-type", "").startswith("application/json")


async def test_b8_3_username_and_text(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    body = next(e for e in receiver.received if e["path"] == "/slack")["body"]
    assert isinstance(body.get("username"), str) and body["username"]
    assert isinstance(body.get("text"), str) and body["text"]


async def test_b8_4_color_strict_hex(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    body = next(e for e in receiver.received if e["path"] == "/slack")["body"]
    color = body["attachments"][0]["color"]
    assert HEX_COLOR.match(color), f"color {color!r} not strict hex"


async def test_b8_5_ts_is_int_not_float(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    body = next(e for e in receiver.received if e["path"] == "/slack")["body"]
    ts = body["attachments"][0]["ts"]
    # Receiver decoded JSON — int stays int, float stays float.
    assert isinstance(ts, int) and not isinstance(ts, bool), f"ts type {type(ts).__name__}"


async def test_b8_6_footer_present(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    body = next(e for e in receiver.received if e["path"] == "/slack")["body"]
    footer = body["attachments"][0].get("footer")
    assert isinstance(footer, str) and footer


async def test_b8_7_required_field_titles(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    body = next(e for e in receiver.received if e["path"] == "/slack")["body"]
    titles = " ".join(f["title"] for f in body["attachments"][0]["fields"]).lower()
    for needle in REQUIRED_TITLES:
        assert needle in titles, f"missing required field title substring: {needle!r}"


async def test_b8_8_resolved_shape(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver)
    await wait_for(lambda: any(
        e["path"] == "/slack" and e["body"].get("text") for e in receiver.received
    ), timeout=5)
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    ok = await wait_for(
        lambda: sum(1 for e in receiver.received if e["path"] == "/slack") >= 2,
        timeout=5,
    )
    assert ok
    resolved = [e for e in receiver.received if e["path"] == "/slack"][1]["body"]
    assert HEX_COLOR.match(resolved["attachments"][0]["color"])
    assert isinstance(resolved["attachments"][0]["ts"], int)


async def test_b8_block_kit_present(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    """Block Kit bonus: payload also contains a ``blocks`` array."""
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    body = next(e for e in receiver.received if e["path"] == "/slack")["body"]
    assert isinstance(body.get("blocks"), list) and body["blocks"]
    types = {b.get("type") for b in body["blocks"]}
    assert "header" in types
    assert "section" in types


async def test_b8_block_kit_field_substrings(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    """Block Kit fields contain the same required substrings as legacy attachments."""
    await _setup_breach(client, proxy_server, receiver)
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    body = next(e for e in receiver.received if e["path"] == "/slack")["body"]
    # Concatenate all text from all blocks
    blob = ""
    for b in body["blocks"]:
        text = b.get("text")
        if isinstance(text, dict):
            blob += " " + text.get("text", "")
        for f in b.get("fields", []) or []:
            if isinstance(f, dict):
                blob += " " + f.get("text", "")
    blob = blob.lower()
    for needle in REQUIRED_TITLES:
        assert needle in blob, f"Block Kit missing substring: {needle!r}"


async def test_b9_discord_embed_has_timestamp(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receivers
) -> None:
    """Discord embed includes ``timestamp`` field that embed validators look for."""
    rcv = receivers()
    await client.post("/config", json={"check_interval_seconds": 1, "request_timeout_ms": 800})
    await client.post("/integrations", json={
        "type": "discord", "webhook_url": rcv.url_for("discord"),
    })
    for i in range(5):
        proxy_server.set(f"px-{i}", status=503 if i < 2 else 200)
    await client.post(
        "/proxies",
        json={"proxies": [proxy_server.url_for(f"px-{i}") for i in range(5)], "replace": True},
    )
    ok = await wait_for(lambda: any(e["path"] == "/discord" for e in rcv.received), timeout=5)
    assert ok
    body = next(e for e in rcv.received if e["path"] == "/discord")["body"]
    ts = body["embeds"][0].get("timestamp")
    assert isinstance(ts, str) and ts


async def test_b8_9_event_filter_only_fired(
    client: httpx.AsyncClient, proxy_server: MockProxyServer, receiver: MockReceiver
) -> None:
    await _setup_breach(client, proxy_server, receiver, events=["alert.fired"])
    ok = await wait_for(lambda: any(e["path"] == "/slack" for e in receiver.received), timeout=5)
    assert ok
    for i in range(5):
        proxy_server.set(f"px-{i}", status=200)
    await asyncio.sleep(2.5)
    # Only one Slack call ever (the fire), no resolve.
    slack_hits = [e for e in receiver.received if e["path"] == "/slack"]
    assert len(slack_hits) == 1
