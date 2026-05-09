"""Spec test 7 — webhook returns 503 twice then 200, exactly one delivery."""
from __future__ import annotations

import httpx
import pytest

import app.dispatcher as dispatcher_mod
from app.dispatcher import _deliver_one, deliver_event
from app.state import Webhook, state


async def test_503_503_then_200_yields_one_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "RETRY_DELAYS", (0, 0, 0, 0))

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(503, json={"err": "later"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await _deliver_one(
            client, "http://r.test/h", {"alert_id": "a"}, "a:alert.fired:wh-1"
        )

    assert len(calls) == 3
    assert state.delivered_keys == {"a:alert.fired:wh-1"}
    assert state.metrics.webhook_deliveries == 1


async def test_redelivery_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "RETRY_DELAYS", (0,))

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        for _ in range(3):
            await _deliver_one(
                client, "http://r.test/h", {"alert_id": "a"}, "a:alert.fired:wh-1"
            )

    assert len(calls) == 1
    assert state.metrics.webhook_deliveries == 1


async def test_event_fanout_uses_correct_idempotency_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "RETRY_DELAYS", (0,))

    received: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        received.append((str(request.url), _json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)

    state.webhooks["wh-A"] = Webhook(webhook_id="wh-A", url="http://a.test/h")
    state.webhooks["wh-B"] = Webhook(webhook_id="wh-B", url="http://b.test/h")

    # Patch httpx.AsyncClient inside the dispatcher to use our transport.
    import httpx as _httpx
    orig = _httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(dispatcher_mod.httpx, "AsyncClient", patched)

    payload = {"event": "alert.fired", "alert_id": "alert-z"}
    await deliver_event({"type": "alert.fired", "payload": payload})

    assert len(received) == 2
    assert state.delivered_keys == {
        "alert-z:alert.fired:wh-A",
        "alert-z:alert.fired:wh-B",
    }
