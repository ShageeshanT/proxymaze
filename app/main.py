"""ProxyMaze FastAPI application entrypoint.

Wires the HTTP routes together and owns the background task lifecycle
(prober + webhook dispatcher) via FastAPI's lifespan context manager.
Routers are added incrementally as each phase is implemented.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.routes import health

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
log = logging.getLogger("proxymaze")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("ProxyMaze starting up")
    # Background prober + dispatcher tasks will be created here in later phases.
    try:
        yield
    finally:
        log.info("ProxyMaze shutting down")


app = FastAPI(
    title="ProxyMaze",
    description="Proxy pool monitoring with alerts and webhook delivery.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
