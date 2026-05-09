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
    """Return True iff the URL is "up".

    Strategy: follow the redirect chain to see the real backend status
    (capture servers commonly return 301 to an HTTPS endpoint that
    encodes the real up/down state). Classification rules:

    - Final response is 2xx → ``up``.
    - Final response is 4xx → ``down`` (server reachable but rejecting).
    - Final response is 5xx → distinguish:
        * 502/503/504 from the upstream after a redirect chain often
          means the redirect target is misconfigured rather than the
          monitored proxy being down. If we *did* receive a 3xx from
          the original URL, treat that 3xx as an "alive" signal and
          mark ``up`` — the proxy responded.
        * Direct 5xx (no redirect, or 500) → ``down``.
    - Timeout / connection error → ``down``.
    """
    try:
        r = await client.get(
            url, timeout=timeout_s, follow_redirects=True, headers=_PROBE_HEADERS,
        )
        if 200 <= r.status_code < 300:
            return True
        if r.status_code == 502 and r.history:
            # Specifically: a redirect chain that ends in 502 Bad Gateway
            # is almost always an infrastructure issue (upstream of a CDN
            # or proxy can't be reached), not a real "service down" from
            # the monitored proxy's perspective. The 3xx was the
            # monitored proxy responding alive. 503 (Service Unavailable)
            # and 504 (Gateway Timeout) ARE meaningful "down" signals
            # — we don't override those.
            for hop in r.history:
                if 300 <= hop.status_code < 400:
                    return True
        return False
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
