"""GET / POST /config — read and update monitoring config.

Updates take effect on the next probe cycle (the prober reads the
current config at the start of each tick rather than caching it).
"""
from __future__ import annotations

from fastapi import APIRouter

from app.models import ConfigBody
from app.state import state

router = APIRouter()


def _as_dict() -> dict[str, int]:
    return {
        "check_interval_seconds": state.config.check_interval_seconds,
        "request_timeout_ms": state.config.request_timeout_ms,
    }


@router.get("/config")
async def get_config() -> dict[str, int]:
    return _as_dict()


@router.post("/config")
async def post_config(body: ConfigBody) -> dict[str, int]:
    state.config.check_interval_seconds = body.check_interval_seconds
    state.config.request_timeout_ms = body.request_timeout_ms
    return _as_dict()
