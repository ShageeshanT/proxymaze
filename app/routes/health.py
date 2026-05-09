"""Liveness endpoint."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/")
async def root() -> dict:
    return {
        "service": "ProxyMaze",
        "status": "ok",
        "endpoints": [
            "GET /health",
            "GET /config",
            "POST /config",
            "POST /proxies",
            "GET /proxies",
            "GET /proxies/{id}",
            "GET /proxies/{id}/history",
            "DELETE /proxies",
            "GET /alerts",
            "POST /webhooks",
            "POST /integrations",
            "GET /metrics",
        ],
    }
