"""Slack and Discord payload formatters.

Pure functions — given an integration record + the in-house alert
payload, return the receiver-shaped JSON dict. The dispatcher imports
``format_for_integration`` and POSTs the returned dict directly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.state import Alert, Integration, state

# Slack hex colours per spec 7.3 (alert.fired = red, alert.resolved = green)
SLACK_COLOR_FIRED = "#FF0000"
SLACK_COLOR_RESOLVED = "#36A64F"

# Discord decimal colours (must be integer in [0, 16777215]) per spec 7.4
DISCORD_COLOR_FIRED = 15158332   # #E74C3C — red
DISCORD_COLOR_RESOLVED = 3066993  # #2ECC71 — green


def _to_ts(iso: str | None) -> int:
    """Convert an ISO 'YYYY-MM-DDTHH:MM:SSZ' to integer Unix seconds."""
    if not iso:
        return int(datetime.now(timezone.utc).timestamp())
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return int(datetime.now(timezone.utc).timestamp())


def _percent(rate: float) -> str:
    return f"{int(round(rate * 100))}%"


def _find_alert(alert_id: str) -> Alert | None:
    return next((a for a in state.alerts if a.alert_id == alert_id), None)


def format_slack(integ: Integration, event_type: str, payload: dict) -> dict:
    """Spec 7.3: hex color, integer ts, required field titles."""
    base: dict = {"username": integ.username or "ProxyMaze"}
    if event_type == "alert.fired":
        base["text"] = payload.get("message", "Proxy pool failure rate exceeded threshold")
        fired_at = payload.get("fired_at", "")
        base["attachments"] = [{
            "color": SLACK_COLOR_FIRED,
            "fields": [
                {"title": "Alert ID", "value": payload.get("alert_id", "")},
                {"title": "Failure Rate", "value": _percent(payload.get("failure_rate", 0))},
                {"title": "Failed Proxies", "value": str(payload.get("failed_proxies", 0))},
                {"title": "Threshold", "value": _percent(payload.get("threshold", 0.2))},
                {"title": "Failed IDs", "value": ", ".join(payload.get("failed_proxy_ids", []))},
                {"title": "Fired At", "value": fired_at},
            ],
            "footer": "ProxyMaze",
            "ts": _to_ts(fired_at),
        }]
        return base

    # alert.resolved — enrich from state for richer fields
    alert = _find_alert(payload.get("alert_id", ""))
    resolved_at = payload.get("resolved_at", "")
    fired_at = alert.fired_at if alert else ""
    failed_ids = alert.failed_ids_at_resolve or [] if alert else []
    failed_count = alert.failed_at_resolve if alert and alert.failed_at_resolve is not None else 0
    failure_rate = alert.failure_rate_at_resolve if alert and alert.failure_rate_at_resolve is not None else 0.0
    base["text"] = "Proxy pool failure rate recovered"
    base["attachments"] = [{
        "color": SLACK_COLOR_RESOLVED,
        "fields": [
            {"title": "Alert ID", "value": payload.get("alert_id", "")},
            {"title": "Failure Rate", "value": _percent(failure_rate)},
            {"title": "Failed Proxies", "value": str(failed_count)},
            {"title": "Threshold", "value": _percent(0.2)},
            {"title": "Failed IDs", "value": ", ".join(failed_ids)},
            {"title": "Fired At", "value": fired_at},
            {"title": "Resolved At", "value": resolved_at},
        ],
        "footer": "ProxyMaze",
        "ts": _to_ts(resolved_at),
    }]
    return base


def format_discord(integ: Integration, event_type: str, payload: dict) -> dict:
    """Spec 7.4: integer color, fields with name (not title)."""
    if event_type == "alert.fired":
        return {
            "embeds": [{
                "title": "Proxy alert fired",
                "description": payload.get("message", "Proxy pool failure rate exceeded threshold"),
                "color": DISCORD_COLOR_FIRED,
                "fields": [
                    {"name": "Alert ID", "value": payload.get("alert_id", "")},
                    {"name": "Failure Rate", "value": _percent(payload.get("failure_rate", 0))},
                    {"name": "Failed Proxies", "value": str(payload.get("failed_proxies", 0))},
                    {"name": "Threshold", "value": _percent(payload.get("threshold", 0.2))},
                    {"name": "Failed IDs", "value": ", ".join(payload.get("failed_proxy_ids", []))},
                ],
                "footer": {"text": "ProxyMaze"},
            }]
        }
    alert = _find_alert(payload.get("alert_id", ""))
    failed_ids = alert.failed_ids_at_resolve or [] if alert else []
    failed_count = alert.failed_at_resolve if alert and alert.failed_at_resolve is not None else 0
    failure_rate = alert.failure_rate_at_resolve if alert and alert.failure_rate_at_resolve is not None else 0.0
    return {
        "embeds": [{
            "title": "Proxy alert resolved",
            "description": "Proxy pool failure rate recovered",
            "color": DISCORD_COLOR_RESOLVED,
            "fields": [
                {"name": "Alert ID", "value": payload.get("alert_id", "")},
                {"name": "Failure Rate", "value": _percent(failure_rate)},
                {"name": "Failed Proxies", "value": str(failed_count)},
                {"name": "Threshold", "value": _percent(0.2)},
                {"name": "Failed IDs", "value": ", ".join(failed_ids)},
                {"name": "Resolved At", "value": payload.get("resolved_at", "")},
            ],
            "footer": {"text": "ProxyMaze"},
        }]
    }


def format_for_integration(integ: Integration, event_type: str, payload: dict) -> dict:
    if integ.type == "slack":
        return format_slack(integ, event_type, payload)
    if integ.type == "discord":
        return format_discord(integ, event_type, payload)
    return payload
