"""ProxyMaze FastAPI application entrypoint.

Wires the HTTP routes together and owns the background task lifecycle
(prober + webhook dispatcher) via FastAPI's lifespan context manager.
Routers are added incrementally as each phase is implemented.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.dispatcher import dispatcher_loop
from app.prober import prober_loop
from app.routes import alerts, config, health, integrations, metrics, proxies, webhooks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("proxymaze")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("ProxyMaze starting up")
    prober_task = asyncio.create_task(prober_loop(), name="prober")
    dispatcher_task = asyncio.create_task(dispatcher_loop(), name="dispatcher")
    try:
        yield
    finally:
        log.info("ProxyMaze shutting down")
        for task in (prober_task, dispatcher_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(
    title="ProxyMaze",
    description="Proxy pool monitoring with alerts and webhook delivery.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(config.router)
app.include_router(proxies.router)
app.include_router(alerts.router)
app.include_router(webhooks.router)
app.include_router(integrations.router)
app.include_router(metrics.router)
