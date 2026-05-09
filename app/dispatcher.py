"""Webhook dispatcher worker.

Drains the FIFO event queue produced by ``app.alerts``. For each event,
fans out to every registered plain webhook and integration in parallel.

Key invariants:

* **Order**: the queue is processed serially, so on a re-breach
  ``alert.resolved`` for the old alert is delivered before
  ``alert.fired`` for the new alert (spec §8 / 5.6).
* **Exactly-once**: every (alert_id, event_type, receiver_id) tuple is
  attempted until it lands a 2xx, but only ONE successful delivery is
  ever counted (idempotency key is added to ``state.delivered_keys``).
* **Deadline**: each event has a hard 60s budget measured from the
  moment of the underlying state transition (when it was enqueued),
  per spec §7.0.4.
* **Retries**: only on transient HTTP failures (5xx & 408/425/429) and
  network errors; 4xx is non-retryable per spec §7.0.4.

A single long-lived ``httpx.AsyncClient`` is reused across all events
so we don't pay TLS-handshake / DNS-lookup costs per delivery — that
matters on cold cloud hosts where the first connection can be slow.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.state import state

log = logging.getLogger("proxymaze.dispatcher")

# Per-attempt request timeout. Kept tight so the retry loop fits in
# the 60s deadline even when the receiver hangs on every attempt.
REQUEST_TIMEOUT_S = 5.0

# Backoff schedule (seconds). Each delay is clamped to whatever budget
# remains on the 60s deadline so we never overshoot.
RETRY_DELAYS: tuple[int, ...] = (1, 2, 4, 8, 16)

# Status codes treated as transient per spec §7.0.4 (500/502/503/504),
# plus the additional standard transient codes (408/425/429).
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

PER_EVENT_DEADLINE_S = 60.0

DISPATCH_HEADERS = {
    "User-Agent": "ProxyMaze-Webhook/1.0",
    "Accept": "*/*",
    "Content-Type": "application/json",
}


def _build_targets(event_type: str, payload: dict) -> list[tuple[str, dict, str]]:
    """Resolve the (url, body, idempotency_key) tuples for an event.

    Plain webhooks always receive the canonical event payload as-is.
    Integrations receive the same payload re-shaped by the corresponding
    formatter (Slack / Discord). Integrations whose ``events`` list does
    not include the current event_type are skipped.
    """
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
        try:
            body = (
                format_for_integration(integ, event_type, payload)
                if format_for_integration is not None
                else payload
            )
        except Exception:  # noqa: BLE001 — never let formatter bugs nuke delivery
            log.exception("integration formatter failed for %s; sending raw", integ.type)
            body = payload
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


async def _deliver_one(
    client: httpx.AsyncClient, url: str, body: dict, key: str, deadline: float
) -> None:
    """POST ``body`` to ``url`` with retries until it succeeds, the
    deadline elapses, or a non-retryable response is returned.
    Idempotent via ``state.delivered_keys``."""
    if key in state.delivered_keys:
        return
    attempt = 0
    log.info("dispatch start %s -> %s", key, url)
    while time.monotonic() < deadline:
        try:
            r = await client.post(
                url,
                json=body,
                timeout=REQUEST_TIMEOUT_S,
                headers=DISPATCH_HEADERS,
                follow_redirects=False,
            )
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
                    "retry %s in %.2fs (status=%d, remaining=%.1fs, attempt=%d)",
                    key, sleep_time, r.status_code, remaining, attempt + 1,
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
                "retry %s in %.2fs (err=%s: %s, remaining=%.1fs, attempt=%d)",
                key, sleep_time, type(e).__name__, e, remaining, attempt + 1,
            )
            await asyncio.sleep(sleep_time)
            attempt += 1
            continue
    log.warning("drop %s (60s deadline reached after %d attempts)", key, attempt + 1)


async def deliver_event(client: httpx.AsyncClient, event: dict) -> None:
    event_type = event["type"]
    payload = event.get("payload")
    if not payload:
        log.warning("no payload for %s; skipping", event_type)
        return

    targets = _build_targets(event_type, payload)
    if not targets:
        log.info("no receivers for %s; skipping", event_type)
        return
    log.info("dispatching %s to %d receiver(s)", event_type, len(targets))

    queued_at = event.get("queued_at", time.monotonic())
    deadline = queued_at + PER_EVENT_DEADLINE_S

    await asyncio.gather(
        *(_deliver_one(client, url, body, key, deadline)
          for url, body, key in targets),
        return_exceptions=True,
    )


async def dispatcher_loop() -> None:
    """One long-lived httpx client across all events. Reusing connections
    reduces per-delivery latency on cold cloud hosts and keeps DNS / TLS
    state warm between events."""
    log.info("dispatcher loop starting")
    timeout = httpx.Timeout(REQUEST_TIMEOUT_S, connect=REQUEST_TIMEOUT_S)
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    try:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
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
                    await deliver_event(client, event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("dispatch failed for event %s", event.get("type"))
    except asyncio.CancelledError:
        log.info("dispatcher loop cancelled")
        raise
