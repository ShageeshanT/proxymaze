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

# Look like a normal HTTP client so capture servers that filter on
# user-agent don't reject our probes.
_PROBE_HEADERS = {
    "User-Agent": "ProxyMaze/1.0 (+https://github.com/proxymaze)",
    "Accept": "*/*",
}


async def _probe_one(client: httpx.AsyncClient, url: str, timeout_s: float) -> bool:
    """Return True iff the URL replied 2xx OR 3xx within the timeout.

    Redirect responses (301/302/etc) count as ``up`` — the server is
    alive and responding. We deliberately do NOT follow redirects:
    capture servers under test commonly return a 301 to an HTTPS
    endpoint that may be unreachable from this network (502 Bad
    Gateway), and following the chain would misclassify every healthy
    proxy as ``down``. A 4xx, 5xx, timeout, or connection error stays
    ``down``.
    """
    try:
        r = await client.get(
            url, timeout=timeout_s, follow_redirects=False, headers=_PROBE_HEADERS,
        )
        return 200 <= r.status_code < 400
    except Exception:
        return False


async def probe_pool(proxies: Iterable[Proxy], timeout_ms: int) -> None:
    targets = list(proxies)
    if not targets:
        return
    timeout_s = timeout_ms / 1000
    # Generous connection pool so 200+ concurrent probes don't queue.
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=100)
    async with httpx.AsyncClient(limits=limits) as client:
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
    """Forever-loop. Re-reads config every tick AND sleeps in 0.5s
    chunks so a freshly POSTed ``check_interval_seconds`` takes effect
    within ~half a second instead of after the previous full sleep."""
    log.info("prober loop starting")
    try:
        while True:
            try:
                await probe_pool(
                    list(state.proxies.values()), state.config.request_timeout_ms
                )
                await evaluate_alerts()
            except Exception:
                log.exception("probe cycle failed")
            # Sleep in 0.5s chunks so config changes take effect within
            # ~half a second. Also break out early if a route handler
            # signals that the pool changed (POST/DELETE /proxies) — this
            # gives newly-added proxies a real status almost immediately
            # instead of leaving them in ``pending`` for up to one full
            # interval.
            elapsed = 0.0
            while elapsed < state.config.check_interval_seconds:
                step = min(0.5, state.config.check_interval_seconds - elapsed)
                try:
                    await asyncio.wait_for(state.wake_event.wait(), timeout=step)
                    state.wake_event.clear()
                    break
                except asyncio.TimeoutError:
                    pass
                elapsed += step
    except asyncio.CancelledError:
        log.info("prober loop cancelled")
        raise
