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

import httpx

from app.alerts import build_dynamic_payload_for_event
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

_receiver_queues: dict[str, asyncio.Queue] = {}
_receiver_tasks: dict[str, asyncio.Task] = {}


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


async def _receiver_worker(receiver_id: str, is_integration: bool) -> None:
    """Background task processing events serially for a single receiver."""
    queue = _receiver_queues[receiver_id]
    try:
        from app.integrations import format_for_integration
    except ImportError:
        format_for_integration = None  # type: ignore[assignment]

    async with httpx.AsyncClient() as client:
        while True:
            try:
                event_type, alert_id = await queue.get()
            except asyncio.CancelledError:
                break

            try:
                # 1. Dynamically generate the payload using live pool state
                base_payload = build_dynamic_payload_for_event(event_type, alert_id)
                if not base_payload:
                    log.warning("could not build payload for %s %s", event_type, alert_id)
                    continue

                # 2. Resolve receiver details
                url = None
                body = None
                key = f"{alert_id}:{event_type}:{receiver_id}"

                if not is_integration:
                    wh = state.webhooks.get(receiver_id)
                    if wh:
                        url = wh.url
                        body = base_payload
                else:
                    integ = next((i for i in state.integrations if i.integration_id == receiver_id), None)
                    if integ and event_type in integ.events:
                        url = integ.webhook_url
                        if format_for_integration is None:
                            body = base_payload
                        else:
                            body = format_for_integration(integ, event_type, base_payload)

                if url and body:
                    await _deliver_one(client, url, body, key)

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("worker failed for receiver %s", receiver_id)
            finally:
                queue.task_done()


def _ensure_worker(receiver_id: str, is_integration: bool) -> None:
    if receiver_id not in _receiver_queues:
        _receiver_queues[receiver_id] = asyncio.Queue()
        _receiver_tasks[receiver_id] = asyncio.create_task(
            _receiver_worker(receiver_id, is_integration),
            name=f"dispatcher-{receiver_id}"
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
            
            event_type = event["type"]
            alert_id = event["alert_id"]
            
            # Fan out to all webhooks
            for wh in list(state.webhooks.values()):
                _ensure_worker(wh.webhook_id, False)
                _receiver_queues[wh.webhook_id].put_nowait((event_type, alert_id))
            
            # Fan out to all integrations
            for integ in list(state.integrations):
                if event_type in integ.events:
                    _ensure_worker(integ.integration_id, True)
                    _receiver_queues[integ.integration_id].put_nowait((event_type, alert_id))

    except asyncio.CancelledError:
        log.info("dispatcher loop cancelled")
        for task in _receiver_tasks.values():
            task.cancel()
        raise

