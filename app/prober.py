"""Background probe loop.

Runs forever from the FastAPI lifespan task. Each tick re-reads config
(so updates take effect mid-flight, per spec rule 5.10), probes every
proxy in the pool concurrently, mutates per-proxy state, then triggers
alert evaluation.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx

from app.alerts import evaluate_alerts
from app.state import HistoryEntry, Proxy, state
from app.utils import utcnow_iso

log = logging.getLogger("proxymaze.prober")

# Cap per-proxy history to keep memory bounded across long runs.
HISTORY_CAP = 100


async def _probe_one(client: httpx.AsyncClient, url: str, timeout_s: float) -> bool:
    """Return True iff the URL replied 2xx within the timeout."""
    try:
        r = await client.get(url, timeout=timeout_s)
        return 200 <= r.status_code < 300
    except Exception:
        return False


async def probe_pool(proxies: Iterable[Proxy], timeout_ms: int) -> None:
    targets = list(proxies)
    if not targets:
        return
    timeout_s = timeout_ms / 1000
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_probe_one(client, p.url, timeout_s) for p in targets),
            return_exceptions=False,
        )
    now = utcnow_iso()
    for proxy, ok in zip(targets, results):
        proxy.status = "up" if ok else "down"
        proxy.last_checked_at = now
        proxy.total_checks += 1
        state.metrics.total_checks += 1
        if ok:
            proxy.up_count += 1
            proxy.consecutive_failures = 0
        else:
            proxy.consecutive_failures += 1
        proxy.history.append(HistoryEntry(checked_at=now, status=proxy.status))
        if len(proxy.history) > HISTORY_CAP:
            del proxy.history[: len(proxy.history) - HISTORY_CAP]


async def prober_loop() -> None:
    log.info("prober loop starting")
    try:
        while True:
            cfg = state.config
            try:
                await probe_pool(list(state.proxies.values()), cfg.request_timeout_ms)
                await evaluate_alerts()
            except Exception:
                log.exception("probe cycle failed")
            await asyncio.sleep(cfg.check_interval_seconds)
    except asyncio.CancelledError:
        log.info("prober loop cancelled")
        raise
