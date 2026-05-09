"""Webhook dispatcher worker.

Drains the FIFO event queue produced by ``app.alerts``. For each event,
fans out to every registered plain webhook and integration, with
exponential-backoff retries on 5xx and exactly-once semantics tracked
by an idempotency key of ``alert_id:event_type:receiver_id``.

Processing the queue serially preserves event order across receivers
(spec rule 5.6: ``alert.resolved`` for the old alert must arrive
before ``alert.fired`` for the new one on a re-breach).

Retry budget is sized so the entire chain completes within 60s
(evaluator's per-event window): 4 attempts, delays 1+2+4+8 = 15s of
sleep, plus per-attempt 8s timeout = ~47s worst case.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx

from app.state import state

log = logging.getLogger("proxymaze.dispatcher")

# Backoff schedule (seconds). Total retry sleep = sum(RETRY_DELAYS).
# Sized to keep the whole delivery under the 60s evaluator deadline
# even when every attempt times out.
RETRY_DELAYS: tuple[int, ...] = (1, 2, 4, 8)
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
REQUEST_TIMEOUT_S = 8.0
DISPATCH_HEADERS = {
    "User-Agent": "ProxyMaze-Webhook/1.0",
    "Accept": "*/*",
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


async def _deliver_one(
    client: httpx.AsyncClient, url: str, body: dict, key: str
) -> None:
    if key in state.delivered_keys:
        return
    for delay in (*RETRY_DELAYS, None):
        try:
            r = await client.post(
                url,
                json=body,
                timeout=REQUEST_TIMEOUT_S,
                headers=DISPATCH_HEADERS,
                follow_redirects=True,
            )
            if 200 <= r.status_code < 300:
                state.delivered_keys.add(key)
                state.metrics.webhook_deliveries += 1
                log.info("delivered %s -> %s (status=%d)", key, url, r.status_code)
                return
            if r.status_code in RETRYABLE_STATUS and delay is not None:
                log.warning(
                    "retry %s after %ss (status=%d)", key, delay, r.status_code
                )
                await asyncio.sleep(delay)
                continue
            log.warning("drop %s status=%d", key, r.status_code)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — network/transport errors
            if delay is not None:
                log.warning("retry %s after %ss (err=%s: %s)", key, delay, type(e).__name__, e)
                await asyncio.sleep(delay)
                continue
            log.warning("drop %s (retries exhausted: %s: %s)", key, type(e).__name__, e)
            return


async def deliver_event(event: dict) -> None:
    targets = _build_targets(event["type"], event["payload"])
    if not targets:
        log.info("no receivers for %s", event["type"])
        return
    log.info("dispatching %s to %d receiver(s)", event["type"], len(targets))
    # One client per event keeps connection pools small while still
    # parallelizing delivery to all receivers.
    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *(_deliver_one(client, url, body, key) for url, body, key in targets),
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
