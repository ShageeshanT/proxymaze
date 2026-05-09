"""Shared pytest fixtures for the ProxyMaze test suite.

Architecture: each test gets a fresh module-level state (autouse
``_reset_state``), and async tests can opt into a real running service
via the ``client`` fixture, which:

  1. starts the FastAPI lifespan (so the prober + dispatcher tasks run
     in the same event loop as the test — no cross-loop queue bugs);
  2. exposes an ``httpx.AsyncClient`` bound to the in-memory ASGI app.

Mock proxy / receiver servers are real HTTP services so the prober and
dispatcher exercise real outbound networking.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

import app.dispatcher as dispatcher_mod
from app.main import app as fastapi_app
from app.state import state

from tests.helpers.mock_servers import MockProxyServer, MockReceiver

# Quieten noisy libraries during tests so failures stay readable.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.ERROR)
logging.getLogger("uvicorn.access").setLevel(logging.ERROR)


# ---------------------------------------------------------------- state


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Fresh state object before AND after every test."""
    state.reset()
    yield
    state.reset()


@pytest.fixture(autouse=True)
def _fast_dispatch_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests don't want to wait 1+2+4+... seconds on retry paths."""
    monkeypatch.setattr(dispatcher_mod, "RETRY_DELAYS", (0, 0, 0, 0, 0, 0, 0))


@pytest.fixture(autouse=True)
def _fast_config() -> None:
    """Default fast probe interval; individual tests can still override."""
    state.config.check_interval_seconds = 1
    state.config.request_timeout_ms = 800


# ---------------------------------------------------------------- client


@pytest_asyncio.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """Async client with the FastAPI lifespan running in the test loop.

    Use this whenever a test needs the prober or dispatcher to actually
    run; for purely-synchronous shape checks the lighter ``no_lifespan_client``
    is faster.
    """
    async with LifespanManager(fastapi_app):
        transport = httpx.ASGITransport(app=fastapi_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=10
        ) as c:
            yield c


@pytest_asyncio.fixture
async def no_lifespan_client() -> AsyncIterator[httpx.AsyncClient]:
    """Client that does NOT start the prober/dispatcher loops — for tests
    that drive ``evaluate_alerts`` manually and need deterministic state."""
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=10
    ) as c:
        yield c


# ---------------------------------------------------------------- mocks


@pytest.fixture
def proxy_server() -> Iterator[MockProxyServer]:
    s = MockProxyServer()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def receiver() -> Iterator[MockReceiver]:
    s = MockReceiver()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def receivers() -> Iterator[callable]:
    """Factory that creates multiple mock receivers and tears them all down."""
    spawned: list[MockReceiver] = []

    def make() -> MockReceiver:
        r = MockReceiver()
        spawned.append(r)
        return r

    try:
        yield make
    finally:
        for r in spawned:
            r.stop()


# ---------------------------------------------------------------- helpers


async def wait_for(predicate, timeout: float = 5.0, step: float = 0.05) -> bool:
    """Poll ``predicate()`` until it returns truthy or the timeout elapses."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


@pytest.fixture
def waiter():
    return wait_for
