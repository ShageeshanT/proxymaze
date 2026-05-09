"""Pydantic schemas for request/response bodies.

Every request model uses ``extra="ignore"`` so unknown fields in the
payload are silently dropped rather than rejected — the spec requires
this tolerance for every JSON object body.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    check_interval_seconds: int = Field(default=15, ge=1)
    request_timeout_ms: int = Field(default=3000, ge=1)


class ProxiesBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    proxies: list[str] = Field(default_factory=list)
    replace: bool = False


class WebhookBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str


class IntegrationBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["slack", "discord"]
    webhook_url: str
    username: str | None = None
    events: list[str] = Field(default_factory=lambda: ["alert.fired", "alert.resolved"])
