"""Webhook dispatcher worker.

Drains the FIFO event queue produced by ``app.alerts``. For each event,
fans out to every registered plain webhook and integration, with
exponential-backoff retries on 5xx and exactly-once semantics tracked
by an idempotency key of ``alert_id:event_type:receiver_id``.

Processing the queue serially preserves event order across receivers
(spec rule 5.6: ``alert.resolved`` for the old alert must arrive
before ``alert.fired`` for the new one on a re-breach).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx

from app.state import state

log = logging.getLogger("proxymaze.dispatcher")

# Exponential backoff schedule (seconds). After the last delay the
# delivery is dropped; total cap is well under the 5-minute spec budget.
RETRY_DELAYS: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 60)
RETRYABLE_STATUS = {500, 502, 503, 504}
REQUEST_TIMEOUT_S = 10.0


def _build_targets(event_type: str, payload: dict) -> list[tuple[str, dict, str]]:
    """Resolve the (url, body, idempotency_key) tuples for an event."""
    alert_id = payload.get("alert_id", "unknown")
    targets: list[tuple[str, dict, str]] = []
    for wh in list(state.webhooks.values()):
        targets.append(
            (wh.url, payload, f"{alert_id}:{event_type}:{wh.webhook_id}")
        )
    # Integration formatters are wired in Phases 7-8.
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
            r = await client.post(url, json=body, timeout=REQUEST_TIMEOUT_S)
            if 200 <= r.status_code < 300:
                state.delivered_keys.add(key)
                state.metrics.webhook_deliveries += 1
                log.info("delivered %s -> %s", key, url)
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
        except Exception as e:
            if delay is not None:
                log.warning("retry %s after %ss (err=%s)", key, delay, e)
                await asyncio.sleep(delay)
                continue
            log.warning("drop %s (retries exhausted: %s)", key, e)
            return


async def deliver_event(event: dict) -> None:
    targets = _build_targets(event["type"], event["payload"])
    if not targets:
        return
    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *(_deliver_one(client, url, body, key) for url, body, key in targets)
        )


async def dispatcher_loop() -> None:
    log.info("dispatcher loop starting")
    try:
        while True:
            event = await state.event_queue.get()
            try:
                await deliver_event(event)
            except Exception:
                log.exception("dispatch failed for event %s", event.get("type"))
    except asyncio.CancelledError:
        log.info("dispatcher loop cancelled")
        raise
