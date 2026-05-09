"""Spec tests 1, 2, 3, 9 — basic endpoint shapes + extra-field tolerance."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.state import Alert, state


def test_health() -> None:
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_config_roundtrips() -> None:
    c = TestClient(app)
    assert c.get("/config").json() == {
        "check_interval_seconds": 15, "request_timeout_ms": 3000,
    }
    r = c.post("/config", json={"check_interval_seconds": 7, "request_timeout_ms": 1500})
    assert r.status_code == 200
    assert r.json() == {"check_interval_seconds": 7, "request_timeout_ms": 1500}
    assert c.get("/config").json() == {
        "check_interval_seconds": 7, "request_timeout_ms": 1500,
    }


def test_proxies_replace_clears_pool_but_keeps_alerts() -> None:
    c = TestClient(app)
    c.post("/proxies", json={"proxies": ["https://x.test/proxy/px-1"]})
    # Plant an alert in the archive so we can verify it survives a replace.
    state.alerts.append(Alert(
        alert_id="alert-keep", fired_at="t", resolved_at="t2",
        failure_rate_at_fire=0.5, total_at_fire=2, failed_at_fire=1, failed_ids_at_fire=["px-1"],
        failure_rate_at_resolve=0.0, total_at_resolve=2, failed_at_resolve=0, failed_ids_at_resolve=[],
    ))

    r = c.post("/proxies", json={
        "proxies": ["https://x.test/proxy/px-99"], "replace": True,
    })
    assert r.status_code == 201
    body = c.get("/proxies").json()
    assert body["total"] == 1
    assert body["proxies"][0]["id"] == "px-99"
    # Alerts archive must be untouched.
    assert any(a["alert_id"] == "alert-keep" for a in c.get("/alerts").json())


def test_proxies_accepts_unknown_fields() -> None:
    c = TestClient(app)
    r = c.post("/proxies", json={
        "proxies": ["https://x.test/proxy/px-1"],
        "replace": False,
        "mystery": True,
        "nested": {"foo": 1},
    })
    assert r.status_code == 201

    # POST /webhooks too
    r = c.post("/webhooks", json={"url": "https://r.test/h", "label": "ignored"})
    assert r.status_code == 201

    # POST /integrations too
    r = c.post("/integrations", json={
        "type": "slack", "webhook_url": "https://hooks.slack.com/x", "extra": "ok",
    })
    assert r.status_code == 201


def test_delete_proxies_returns_204_and_keeps_alerts() -> None:
    c = TestClient(app)
    c.post("/proxies", json={"proxies": ["https://x.test/proxy/px-1"]})
    state.alerts.append(Alert(
        alert_id="alert-keep", fired_at="t", resolved_at="t2",
        failure_rate_at_fire=0.5, total_at_fire=1, failed_at_fire=1, failed_ids_at_fire=["px-1"],
        failure_rate_at_resolve=0.0, total_at_resolve=1, failed_at_resolve=0, failed_ids_at_resolve=[],
    ))
    r = c.delete("/proxies")
    assert r.status_code == 204
    assert c.get("/proxies").json()["total"] == 0
    assert len(c.get("/alerts").json()) == 1
