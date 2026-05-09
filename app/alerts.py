"""Alert engine.

Evaluated after every probe cycle. Holds the alert lock so concurrent
probe ticks (or test invocations) can't race on fire/resolve. Enforces
the single-active-alert invariant: at most one alert may be active at a
time, and a fresh breach after resolve mints a brand-new alert_id.
"""
from __future__ import annotations

import logging
import uuid
from typing import Iterable

from app.state import Alert, Proxy, state
from app.utils import utcnow_iso

log = logging.getLogger("proxymaze.alerts")

THRESHOLD: float = 0.20
MESSAGE: str = "Proxy pool failure rate exceeded threshold"


def _new_alert_id() -> str:
    return f"alert-{uuid.uuid4().hex[:6]}"


def _pool_snapshot() -> tuple[int, int, list[str], float]:
    """(total, failed, sorted failed_ids, failure_rate) from current state."""
    total = len(state.proxies)
    failed_ids = sorted(p.id for p in state.proxies.values() if p.status == "down")
    failed = len(failed_ids)
    failure_rate = round(failed / total, 4) if total else 0.0
    return total, failed, failed_ids, failure_rate


def _build_fired_payload(alert: Alert) -> dict:
    return {
        "event": "alert.fired",
        "alert_id": alert.alert_id,
        "fired_at": alert.fired_at,
        "failure_rate": alert.failure_rate_at_fire,
        "total_proxies": alert.total_at_fire,
        "failed_proxies": alert.failed_at_fire,
        "failed_proxy_ids": list(alert.failed_ids_at_fire),
        "threshold": THRESHOLD,
        "message": MESSAGE,
    }


def _build_resolved_payload(alert: Alert) -> dict:
    return {
        "event": "alert.resolved",
        "alert_id": alert.alert_id,
        "resolved_at": alert.resolved_at,
    }


def build_dynamic_payload_for_event(event_type: str, alert_id: str) -> dict | None:
    """Builds the webhook payload dynamically using live pool values."""
    alert = next((a for a in state.alerts if a.alert_id == alert_id), None)
    if not alert:
        return None

    if event_type == "alert.fired":
        total, failed, failed_ids, failure_rate = _pool_snapshot()
        return {
            "event": "alert.fired",
            "alert_id": alert.alert_id,
            "fired_at": alert.fired_at,
            "failure_rate": failure_rate,
            "total_proxies": total,
            "failed_proxies": failed,
            "failed_proxy_ids": failed_ids,
            "threshold": THRESHOLD,
            "message": MESSAGE,
        }
    elif event_type == "alert.resolved":
        return {
            "event": "alert.resolved",
            "alert_id": alert.alert_id,
            "resolved_at": alert.resolved_at,
        }
    return None

async def _enqueue(event_type: str, payload: dict) -> None:
    import time
    await state.event_queue.put({
        "type": event_type, 
        "payload": payload, 
        "queued_at": time.monotonic()
    })


async def evaluate_alerts() -> None:
    """Inspect pool, fire or resolve as needed, emit webhook events."""
    async with state.alert_lock:
        total, failed, failed_ids, failure_rate = _pool_snapshot()

        if failure_rate >= THRESHOLD and state.current_active_alert_id is None:
            alert = Alert(
                alert_id=_new_alert_id(),
                fired_at=utcnow_iso(),
                resolved_at=None,
                failure_rate_at_fire=failure_rate,
                total_at_fire=total,
                failed_at_fire=failed,
                failed_ids_at_fire=failed_ids,
            )
            state.alerts.append(alert)
            state.current_active_alert_id = alert.alert_id
            log.info(
                "alert FIRED %s rate=%s failed=%d/%d",
                alert.alert_id, failure_rate, failed, total,
            )
            await _enqueue("alert.fired", _build_fired_payload(alert))
            return

        if failure_rate < THRESHOLD and state.current_active_alert_id is not None:
            active_id = state.current_active_alert_id
            alert = next(
                (a for a in state.alerts if a.alert_id == active_id), None
            )
            if alert is not None:
                alert.resolved_at = utcnow_iso()
                alert.failure_rate_at_resolve = failure_rate
                alert.total_at_resolve = total
                alert.failed_at_resolve = failed
                alert.failed_ids_at_resolve = failed_ids
                log.info(
                    "alert RESOLVED %s rate=%s failed=%d/%d",
                    alert.alert_id, failure_rate, failed, total,
                )
                state.current_active_alert_id = None
                await _enqueue("alert.resolved", _build_resolved_payload(alert))


def serialize_alert(alert: Alert) -> dict:
    """Render an alert for GET /alerts.

    Active alerts use live pool values (per spec rule 5.8). Resolved
    alerts return the snapshot frozen at resolve time.
    """
    if alert.alert_id == state.current_active_alert_id:
        total, failed, failed_ids, failure_rate = _pool_snapshot()
        return {
            "alert_id": alert.alert_id,
            "status": "active",
            "failure_rate": failure_rate,
            "total_proxies": total,
            "failed_proxies": failed,
            "failed_proxy_ids": failed_ids,
            "threshold": THRESHOLD,
            "fired_at": alert.fired_at,
            "resolved_at": None,
            "message": MESSAGE,
        }
    return {
        "alert_id": alert.alert_id,
        "status": "resolved",
        "failure_rate": alert.failure_rate_at_resolve
        if alert.failure_rate_at_resolve is not None
        else alert.failure_rate_at_fire,
        "total_proxies": alert.total_at_resolve
        if alert.total_at_resolve is not None
        else alert.total_at_fire,
        "failed_proxies": alert.failed_at_resolve
        if alert.failed_at_resolve is not None
        else alert.failed_at_fire,
        "failed_proxy_ids": list(
            alert.failed_ids_at_resolve
            if alert.failed_ids_at_resolve is not None
            else alert.failed_ids_at_fire
        ),
        "threshold": THRESHOLD,
        "fired_at": alert.fired_at,
        "resolved_at": alert.resolved_at,
        "message": MESSAGE,
    }
