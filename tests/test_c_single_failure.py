"""B3 — Single failure behavior (30 pts)."""
from __future__ import annotations

import re

import httpx
import pytest

from app.prober import probe_pool
from app.state import Proxy, state
from tests.helpers.mock_servers import MockProxyServer, closed_port_url


ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _add(proxy_server: MockProxyServer, pid: str) -> Proxy:
    p = Proxy(id=pid, url=proxy_server.url_for(pid))
    state.proxies[pid] = p
    return p


async def _probe_once(timeout_ms: int = 800) -> None:
    await probe_pool(list(state.proxies.values()), timeout_ms)


async def test_b3_1_200_marks_up(proxy_server: MockProxyServer) -> None:
    proxy_server.set("a", status=200)
    p = _add(proxy_server, "a")
    await _probe_once()
    assert p.status == "up"
    assert p.consecutive_failures == 0


@pytest.mark.parametrize("code", [500, 502, 503, 504])
async def test_b3_2_5xx_marks_down(proxy_server: MockProxyServer, code: int) -> None:
    proxy_server.set("a", status=code)
    p = _add(proxy_server, "a")
    await _probe_once()
    assert p.status == "down", f"status {code} should mark down"
    assert p.consecutive_failures == 1


async def test_b3_6_timeout_marks_down(proxy_server: MockProxyServer) -> None:
    proxy_server.set("a", status=200, delay_ms=2000)
    p = _add(proxy_server, "a")
    await _probe_once(timeout_ms=300)
    assert p.status == "down"


async def test_b3_7_unreachable_marks_down() -> None:
    pid = "dead"
    state.proxies[pid] = Proxy(id=pid, url=closed_port_url())
    await _probe_once(timeout_ms=500)
    assert state.proxies[pid].status == "down"


async def test_b3_8_consecutive_failures_increment(proxy_server: MockProxyServer) -> None:
    proxy_server.set("a", status=503)
    p = _add(proxy_server, "a")
    await _probe_once()
    await _probe_once()
    assert p.consecutive_failures == 2


async def test_b3_9_success_resets_failures(proxy_server: MockProxyServer) -> None:
    proxy_server.set("a", status=503)
    p = _add(proxy_server, "a")
    await _probe_once()
    await _probe_once()
    assert p.consecutive_failures == 2
    proxy_server.set("a", status=200)
    await _probe_once()
    assert p.consecutive_failures == 0
    assert p.status == "up"


async def test_b3_10_last_checked_at_updates(proxy_server: MockProxyServer) -> None:
    proxy_server.set("a", status=200)
    p = _add(proxy_server, "a")
    await _probe_once()
    assert ISO_Z.match(p.last_checked_at), p.last_checked_at


async def test_b3_11_total_checks_increments(proxy_server: MockProxyServer) -> None:
    proxy_server.set("a", status=200)
    p = _add(proxy_server, "a")
    for _ in range(3):
        await _probe_once()
    assert p.total_checks == 3


async def test_b3_12_history_appended_chronologically(proxy_server: MockProxyServer) -> None:
    proxy_server.set("a", status=200)
    p = _add(proxy_server, "a")
    await _probe_once()
    proxy_server.set("a", status=503)
    await _probe_once()
    proxy_server.set("a", status=200)
    await _probe_once()
    statuses = [h.status for h in p.history]
    assert statuses == ["up", "down", "up"]
    timestamps = [h.checked_at for h in p.history]
    assert timestamps == sorted(timestamps)


async def test_b3_redirect_to_502_marks_down() -> None:
    """301 → 502 (Bad Gateway) classifies as ``down`` per spec:
    "any 5xx response ⇒ down". The final status is what matters; we
    never override a 5xx with "alive" inferences from a redirect hop.
    """
    import threading, socket, time
    from contextlib import closing
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import RedirectResponse
    import uvicorn

    sub = FastAPI()

    @sub.get("/proxy/{pid}")
    async def proxy(pid: str):
        return RedirectResponse(url=f"/upstream/{pid}", status_code=301)

    @sub.get("/upstream/{pid}")
    async def upstream(pid: str):
        raise HTTPException(502)

    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = uvicorn.Config(sub, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with closing(socket.socket()) as s:
                s.settimeout(0.2)
                s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.05)
    try:
        state.proxies["redir-1"] = Proxy(id="redir-1", url=f"http://127.0.0.1:{port}/proxy/redir-1")
        await _probe_once(timeout_ms=2000)
        assert state.proxies["redir-1"].status == "down"
    finally:
        server.should_exit = True
        th.join(timeout=2)


async def test_b3_redirect_to_503_marks_down() -> None:
    """301 → 503 (Service Unavailable) classifies as ``down`` —
    503/504 are intentional "down" signals, not infra issues."""
    import threading, socket, time
    from contextlib import closing
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import RedirectResponse
    import uvicorn

    sub = FastAPI()

    @sub.get("/proxy/{pid}")
    async def proxy(pid: str):
        return RedirectResponse(url=f"/upstream/{pid}", status_code=301)

    @sub.get("/upstream/{pid}")
    async def upstream(pid: str):
        raise HTTPException(503)

    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = uvicorn.Config(sub, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with closing(socket.socket()) as s:
                s.settimeout(0.2)
                s.connect(("127.0.0.1", port))
            break
        except OSError:
            time.sleep(0.05)
    try:
        state.proxies["redir-2"] = Proxy(id="redir-2", url=f"http://127.0.0.1:{port}/proxy/redir-2")
        await _probe_once(timeout_ms=2000)
        assert state.proxies["redir-2"].status == "down"
    finally:
        server.should_exit = True
        th.join(timeout=2)


async def test_b3_13_uptime_percentage(
    no_lifespan_client: httpx.AsyncClient, proxy_server: MockProxyServer
) -> None:
    proxy_server.set("a", status=200)
    p = _add(proxy_server, "a")
    # 3 up, 1 down => 75%
    await _probe_once()
    await _probe_once()
    proxy_server.set("a", status=503)
    await _probe_once()
    proxy_server.set("a", status=200)
    await _probe_once()
    body = (await no_lifespan_client.get("/proxies/a")).json()
    assert body["total_checks"] == 4
    assert body["uptime_percentage"] == pytest.approx(75.0, abs=0.1)
