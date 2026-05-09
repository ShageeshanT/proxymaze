"""Proxy pool ingestion + read endpoints.

Newly added proxies start in ``pending`` and only transition to ``up``
or ``down`` via background probes (see ``app/prober.py``). Replacing or
clearing the pool deliberately leaves the alert archive untouched.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.alerts import evaluate_alerts
from app.models import ProxiesBody
from app.state import Proxy, state
from app.utils import extract_proxy_id

router = APIRouter()


def _proxy_summary(p: Proxy) -> dict:
    return {
        "id": p.id,
        "url": p.url,
        "status": p.status,
        "last_checked_at": p.last_checked_at,
        "consecutive_failures": p.consecutive_failures,
    }


def _pool_counts() -> tuple[int, int, int, float]:
    total = len(state.proxies)
    up = sum(1 for p in state.proxies.values() if p.status == "up")
    down = sum(1 for p in state.proxies.values() if p.status == "down")
    failure_rate = round(down / total, 4) if total else 0.0
    return total, up, down, failure_rate


@router.post("/proxies", status_code=status.HTTP_201_CREATED)
async def post_proxies(body: ProxiesBody) -> dict:
    added: list[Proxy] = []
    async with state.state_lock:
        if body.replace:
            state.proxies.clear()
        for url in body.proxies:
            pid = extract_proxy_id(url)
            proxy = Proxy(id=pid, url=url)
            state.proxies[pid] = proxy
            added.append(proxy)
    # A replace can drop currently-down proxies, which may resolve an
    # active alert; re-evaluate so /alerts and webhooks update promptly.
    if body.replace:
        await evaluate_alerts()
    # Wake the prober so newly added proxies get their first probe within
    # ~0.5s instead of after a full check_interval_seconds wait.
    state.wake_event.set()
    return {
        "accepted": len(added),
        "proxies": [
            {"id": p.id, "url": p.url, "status": p.status} for p in added
        ],
    }


@router.get("/proxies")
async def get_proxies() -> dict:
    total, up, down, failure_rate = _pool_counts()
    return {
        "total": total,
        "up": up,
        "down": down,
        "failure_rate": failure_rate,
        "proxies": [_proxy_summary(p) for p in state.proxies.values()],
    }


@router.delete("/proxies", status_code=status.HTTP_204_NO_CONTENT)
async def delete_proxies() -> Response:
    async with state.state_lock:
        state.proxies.clear()
    # An empty pool has failure_rate 0 -> any active alert resolves.
    await evaluate_alerts()
    state.wake_event.set()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/proxies/{proxy_id}")
async def get_proxy(proxy_id: str) -> dict:
    p = state.proxies.get(proxy_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proxy not found")
    uptime = round((p.up_count / p.total_checks) * 100, 1) if p.total_checks else 100.0
    return {
        "id": p.id,
        "url": p.url,
        "status": p.status,
        "last_checked_at": p.last_checked_at,
        "consecutive_failures": p.consecutive_failures,
        "total_checks": p.total_checks,
        "uptime_percentage": uptime,
        "history": [
            {"checked_at": h.checked_at, "status": h.status} for h in p.history
        ],
    }


@router.get("/proxies/{proxy_id}/history")
async def get_proxy_history(proxy_id: str) -> list[dict]:
    p = state.proxies.get(proxy_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proxy not found")
    return [{"checked_at": h.checked_at, "status": h.status} for h in p.history]
