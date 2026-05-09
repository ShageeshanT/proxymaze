"""In-memory state store — single source of truth for the service.

Imported as a module-level singleton (`from app.state import state`). All
mutation happens here; route handlers and background tasks read/write
through this object so behavior stays consistent across components.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

ProxyStatus = Literal["pending", "up", "down"]


@dataclass
class Config:
    check_interval_seconds: int = 15
    request_timeout_ms: int = 3000


@dataclass
class HistoryEntry:
    checked_at: str
    status: ProxyStatus


@dataclass
class Proxy:
    id: str
    url: str
    status: ProxyStatus = "pending"
    last_checked_at: str | None = None
    consecutive_failures: int = 0
    total_checks: int = 0
    up_count: int = 0
    history: list[HistoryEntry] = field(default_factory=list)


@dataclass
class Alert:
    alert_id: str
    fired_at: str
    resolved_at: str | None
    failure_rate_at_fire: float
    total_at_fire: int
    failed_at_fire: int
    failed_ids_at_fire: list[str]
    # Frozen snapshot captured at resolve time (None while still active).
    failure_rate_at_resolve: float | None = None
    total_at_resolve: int | None = None
    failed_at_resolve: int | None = None
    failed_ids_at_resolve: list[str] | None = None


@dataclass
class Webhook:
    webhook_id: str
    url: str


@dataclass
class Integration:
    integration_id: str
    type: Literal["slack", "discord"]
    webhook_url: str
    username: str | None = None
    events: list[str] = field(default_factory=lambda: ["alert.fired", "alert.resolved"])


@dataclass
class Metrics:
    total_checks: int = 0
    webhook_deliveries: int = 0


@dataclass
class State:
    config: Config = field(default_factory=Config)
    proxies: dict[str, Proxy] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    current_active_alert_id: str | None = None
    webhooks: dict[str, Webhook] = field(default_factory=dict)
    integrations: list[Integration] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    delivered_keys: set[str] = field(default_factory=set)
    # Locks are created lazily on first access from inside an event loop
    # so importing this module from sync contexts (e.g. tests) stays safe.
    _alert_lock: asyncio.Lock | None = None
    _state_lock: asyncio.Lock | None = None

    @property
    def alert_lock(self) -> asyncio.Lock:
        if self._alert_lock is None:
            self._alert_lock = asyncio.Lock()
        return self._alert_lock

    @property
    def state_lock(self) -> asyncio.Lock:
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        return self._state_lock

    def reset(self) -> None:
        """Wipe everything — used by tests between cases."""
        self.config = Config()
        self.proxies.clear()
        self.alerts.clear()
        self.current_active_alert_id = None
        self.webhooks.clear()
        self.integrations.clear()
        self.metrics = Metrics()
        self.delivered_keys.clear()
        self._alert_lock = None
        self._state_lock = None


state = State()
