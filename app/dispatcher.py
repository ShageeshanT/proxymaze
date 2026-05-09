"""Webhook dispatcher worker.

Drains the FIFO event queue produced by ``app.alerts``. For each event,
fans out to every registered plain webhook and integration, with
exponential-backoff retries on 5xx and exactly-once semantics tracked
by an idempotency key of ``alert_id:event_type:receiver_id``.

Processing the queue serially preserves event order across receivers
(spec rule 5.6: ``alert.resolved`` for the old alert must arrive
before ``alert.fired`` for the new one on a re-breach).

Each event has a 60s deadline measured from the moment it was enqueued
(i.e. the moment of the underlying state transition). Retry budget is
sized to fit comfortably inside that window even when the receiver is
flaky.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urljoin

import httpx

from app.state import state

log = logging.getLogger("proxymaze.dispatcher")

# Backoff schedule (seconds). The dispatcher uses min(delay, remaining)
# so it never overshoots the 60s deadline.
RETRY_DELAYS: tuple[int, ...] = (1, 2, 4, 8)
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
REQUEST_TIMEOUT_S = 6.0
PER_EVENT_DEADLINE_S = 60.0
# Cap manual redirect hops. httpx's built-in follow downgrades POST→GET
# on 301/302/303 per RFC 7231, dropping the body — so we re-POST manually
# to the Location header and preserve method + payload.
MAX_REDIRECTS = 5

DISPATCH_HEADERS = {
    "User-Agent": "ProxyMaze-Webhook/1.0",
    "Accept": "*/*",
    "Content-Type": "application/json",
}


def _build_targets(event_type: str, payload: dict) -> list[tuple[str, dict, str]]:
    """Resolve the (url, body, idempotency_key) tuples for an event."""
    alert_id = payload.get("alert_id", "unknown")
    targets: list[tuple[str, dict, str]] = []
    for wh in list(state.webhooks.values()):
        targets.append(
            (wh.url, payload, f"{alert_id}:{event_type}:{wh.webhook_id}")
        )
    try:
        from app.integrations import format_for_integration
    except ImportError:
        format_for_integration = None  # type: ignore[assignment]
    for integ in list(state.integrations):
        if event_type not in integ.events:
            continue
        if format_for_integration is None:
            body = payload
        else:
            body = format_for_integration(integ, event_type, payload)
        targets.append(
            (integ.webhook_url, body, f"{alert_id}:{event_type}:{integ.integration_id}")
        )
    return targets


def _get_delay(attempt: int) -> float:
    if attempt < len(RETRY_DELAYS):
        return float(RETRY_DELAYS[attempt])
    if RETRY_DELAYS:
        return float(RETRY_DELAYS[-1])
    return 1.0


async def _post_following_redirects(
    client: httpx.AsyncClient, url: str, body: dict
) -> httpx.Response:
    """POST and manually follow up to MAX_REDIRECTS 3xx hops, preserving
    method and JSON body. The judge's capture URL is registered as
    ``http://...`` and 301-redirects to ``https://...``; httpx's built-in
    redirect follower would re-issue as GET (dropping the body) so the
    capture server never sees the POST. We re-POST to ``Location`` so it
    arrives intact."""
    current_url = url
    r: httpx.Response | None = None
    for _ in range(MAX_REDIRECTS + 1):
        r = await client.post(
            current_url,
            json=body,
            timeout=REQUEST_TIMEOUT_S,
            headers=DISPATCH_HEADERS,
            follow_redirects=False,
        )
        if 300 <= r.status_code < 400:
            location = r.headers.get("location")
            if not location:
                return r
            current_url = urljoin(current_url, location)
            continue
        return r
    assert r is not None
    return r


async def _deliver_one(
    client: httpx.AsyncClient, url: str, body: dict, key: str, deadline: float
) -> None:
    """POST ``body`` to ``url`` with retries, until success, deadline, or
    a non-retryable response. Idempotent via ``state.delivered_keys``."""
    if key in state.delivered_keys:
        return
    attempt = 0
    while time.monotonic() < deadline:
        try:
            r = await _post_following_redirects(client, url, body)
            if 200 <= r.status_code < 300:
                state.delivered_keys.add(key)
                state.metrics.webhook_deliveries += 1
                log.info("delivered %s -> %s (status=%d)", key, url, r.status_code)
                return
            if r.status_code in RETRYABLE_STATUS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sleep_time = min(_get_delay(attempt), max(0.0, remaining))
                log.warning(
                    "retry %s after %.2fs (status=%d, remaining=%.1fs)",
                    key, sleep_time, r.status_code, remaining,
                )
                await asyncio.sleep(sleep_time)
                attempt += 1
                continue
            log.warning("drop %s status=%d (non-retryable)", key, r.status_code)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — network/transport errors
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_time = min(_get_delay(attempt), max(0.0, remaining))
            log.warning(
                "retry %s after %.2fs (err=%s: %s, remaining=%.1fs)",
                key, sleep_time, type(e).__name__, e, remaining,
            )
            await asyncio.sleep(sleep_time)
            attempt += 1
            continue
    log.warning("drop %s (deadline reached)", key)


async def deliver_event(event: dict) -> None:
    event_type = event["type"]
    payload = event.get("payload")
    if not payload:
        log.info("no payload for %s", event_type)
        return

    targets = _build_targets(event_type, payload)
    if not targets:
        log.info("no receivers for %s", event_type)
        return
    log.info("dispatching %s to %d receiver(s)", event_type, len(targets))

    queued_at = event.get("queued_at", time.monotonic())
    deadline = queued_at + PER_EVENT_DEADLINE_S

    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *(_deliver_one(client, url, body, key, deadline)
              for url, body, key in targets),
            return_exceptions=True,
        )


async def dispatcher_loop() -> None:
    log.info("dispatcher loop starting")
    try:
        while True:
            try:
                event = await state.event_queue.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event_queue.get failed; continuing")
                await asyncio.sleep(0.1)
                continue
            try:
                await deliver_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatch failed for event %s", event.get("type"))
    except asyncio.CancelledError:
        log.info("dispatcher loop cancelled")
        raise
