"""GET /metrics — operational counters."""
from __future__ import annotations

from fastapi import APIRouter

from app.state import state

router = APIRouter()


@router.get("/metrics")
async def get_metrics() -> dict:
    return {
        "total_checks": state.metrics.total_checks,
        "current_pool_size": len(state.proxies),
        "active_alerts": 1 if state.current_active_alert_id is not None else 0,
        "total_alerts": len(state.alerts),
        "webhook_deliveries": state.metrics.webhook_deliveries,
    }
