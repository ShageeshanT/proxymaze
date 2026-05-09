"""GET /alerts — list every alert (active + resolved) in fire order."""
from __future__ import annotations

from fastapi import APIRouter

from app.alerts import serialize_alert
from app.state import state

router = APIRouter()


@router.get("/alerts")
async def get_alerts() -> list[dict]:
    return [serialize_alert(a) for a in state.alerts]
