"""Real HTTP mock servers used by the integration tests.

The ProxyMaze prober and dispatcher both make outbound httpx calls, so
they need real TCP endpoints to talk to. These helpers spin tiny
FastAPI apps inside background uvicorn servers on random free ports.

Two server types:

- :class:`MockProxyServer` — serves a configurable proxy URL space
  (``/proxy/{id}``) where each id can be set to return any status code,
  delay before responding, or be marked unreachable (we route to a
  closed port so connection-refused tests work).

- :class:`MockReceiver` — accepts POST webhooks at any path, records
  every request, and returns a scripted sequence of status codes (so
  the "503, 503, 200" exactly-once test is deterministic).

Both servers expose ``url_for(path)`` helpers and a synchronous
``stop()`` so fixtures can tear them down cleanly.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import closing
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ThreadedServer:
    """Common machinery: build app, bind port, run uvicorn in a thread."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(
            app, host="127.0.0.1", port=self.port,
            log_level="error", access_log=False, lifespan="off",
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        # Poll briefly for readiness instead of fixed sleep.
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                    s.settimeout(0.2)
                    s.connect(("127.0.0.1", self.port))
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"mock server on :{self.port} didn't start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=2)


class MockProxyServer(_ThreadedServer):
    """Mock proxy provider serving GET /proxy/{id} with controllable behavior.

    Per-id behavior dict keys (all optional):
      - ``status``: int HTTP status to return (default 200)
      - ``delay_ms``: sleep before responding (use to trigger probe timeouts)
    """

    def __init__(self) -> None:
        app = FastAPI()
        self.behaviors: dict[str, dict[str, Any]] = {}
        self.requests_received: list[tuple[str, float]] = []
        self.calls_per_id: dict[str, int] = {}

        @app.get("/proxy/{pid}")
        async def proxy(pid: str) -> dict[str, Any]:
            self.requests_received.append((pid, time.time()))
            self.calls_per_id[pid] = self.calls_per_id.get(pid, 0) + 1
            b = self.behaviors.get(pid, {"status": 200})
            delay = b.get("delay_ms", 0)
            if delay:
                await asyncio.sleep(delay / 1000)
            code = b.get("status", 200)
            if not (200 <= code < 300):
                raise HTTPException(status_code=code)
            return {"pid": pid, "ok": True}

        super().__init__(app)
        self.start()

    def url_for(self, pid: str) -> str:
        return f"{self.url}/proxy/{pid}"

    def set(self, pid: str, **behavior: Any) -> None:
        self.behaviors[pid] = behavior

    def reset(self) -> None:
        self.behaviors.clear()
        self.requests_received.clear()
        self.calls_per_id.clear()


class MockReceiver(_ThreadedServer):
    """Mock webhook target. Records everything; status codes per path are scripted.

    ``script(path, *codes)`` queues a sequence of statuses to return on
    successive calls to that path. After the queue empties, returns
    ``default_status`` (200 by default).
    """

    def __init__(self) -> None:
        app = FastAPI()
        self.received: list[dict[str, Any]] = []
        self.scripts: dict[str, list[int]] = {}
        self.default_status = 200
        self.lock = threading.Lock()

        @app.post("/{path:path}")
        async def hook(path: str, request: Request) -> dict[str, Any]:
            try:
                body = await request.json()
            except Exception:
                body = None
            entry = {
                "path": "/" + path,
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": body,
                "ts": time.time(),
            }
            with self.lock:
                queue = self.scripts.get("/" + path, [])
                code = queue.pop(0) if queue else self.default_status
                entry["responded_with"] = code
                self.received.append(entry)
            if not (200 <= code < 300):
                raise HTTPException(status_code=code)
            return {"ok": True}

        super().__init__(app)
        self.start()

    def url_for(self, path: str = "hook") -> str:
        path = path.lstrip("/")
        return f"{self.url}/{path}"

    def script(self, path: str, *codes: int) -> None:
        if not path.startswith("/"):
            path = "/" + path
        with self.lock:
            self.scripts[path] = list(codes)

    def reset(self) -> None:
        with self.lock:
            self.received.clear()
            self.scripts.clear()
            self.default_status = 200

    def by_event(self, event_type: str) -> list[dict[str, Any]]:
        out = []
        with self.lock:
            for entry in self.received:
                body = entry.get("body") or {}
                if isinstance(body, dict) and body.get("event") == event_type:
                    out.append(entry)
        return out

    def successful(self) -> list[dict[str, Any]]:
        with self.lock:
            return [e for e in self.received if 200 <= e["responded_with"] < 300]

    def successful_for_event(self, event_type: str) -> list[dict[str, Any]]:
        return [
            e for e in self.successful()
            if isinstance(e.get("body"), dict) and e["body"].get("event") == event_type
        ]


# A port that is closed for the whole test session — connecting to it
# yields ConnectionRefusedError, simulating an unreachable proxy.
_DEAD_PORT_HOLDER = None


def closed_port_url() -> str:
    """Return a URL to a port that nothing is listening on."""
    global _DEAD_PORT_HOLDER
    if _DEAD_PORT_HOLDER is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # immediately release; nobody will rebind in time
        _DEAD_PORT_HOLDER = port
    return f"http://127.0.0.1:{_DEAD_PORT_HOLDER}/proxy/dead"
