"""Shared helpers: ID extraction, timestamp formatting."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse


def utcnow_iso() -> str:
    """Return current UTC time formatted as ISO 8601 with a trailing 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_proxy_id(url: str) -> str:
    """Last non-empty segment of the URL path is the proxy ID."""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return url
    return path.rsplit("/", 1)[-1]
