# ProxyMaze

In-memory HTTP API service that monitors a pool of proxy URLs, fires alerts when the pool failure rate crosses a 20% threshold, and delivers webhook notifications (plain JSON, Slack, or Discord) with retries and exactly-once semantics.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The service listens on `http://127.0.0.1:8000` by default.

## Deploy to Render

Push this repo to GitHub, then in the Render dashboard create a new Web Service pointing at the repo. The included `render.yaml` blueprint sets the build command (`pip install -r requirements.txt`) and start command (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`). The `Procfile` works for Heroku/Railway/Fly.io style deployments as well.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET    | `/health` | Liveness check. |
| GET    | `/config` | Read current monitoring config. |
| POST   | `/config` | Update `check_interval_seconds`, `request_timeout_ms`. |
| POST   | `/proxies` | Load proxy URLs (`replace: true` to clear pool first). |
| GET    | `/proxies` | Pool summary with per-proxy status. |
| GET    | `/proxies/{id}` | Detail + history for a single proxy. |
| GET    | `/proxies/{id}/history` | Raw check history array. |
| DELETE | `/proxies` | Clear pool (alerts preserved). |
| GET    | `/alerts` | All alerts (active and resolved). |
| POST   | `/webhooks` | Register a plain webhook receiver. |
| POST   | `/integrations` | Register a Slack or Discord integration. |
| GET    | `/metrics` | Operational counters. |

### Examples

```bash
# Load proxies
curl -X POST http://localhost:8000/proxies \
  -H 'Content-Type: application/json' \
  -d '{"proxies":["https://example.com/proxy/px-1","https://example.com/proxy/px-2"],"replace":true}'

# Register webhook
curl -X POST http://localhost:8000/webhooks \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://receiver.example/hook"}'

# Register Slack integration
curl -X POST http://localhost:8000/integrations \
  -H 'Content-Type: application/json' \
  -d '{"type":"slack","webhook_url":"https://hooks.slack.com/...","username":"ProxyWatch","events":["alert.fired","alert.resolved"]}'

# Read alerts
curl http://localhost:8000/alerts
```

## Behavior summary

- Background prober probes every proxy on `check_interval_seconds`; config changes apply mid-flight.
- A 2xx response within `request_timeout_ms` marks `up`; everything else marks `down`.
- Failure rate `down/total` ≥ 0.2 fires a single active alert; recovery resolves it.
- A subsequent breach mints a fresh `alert_id`; resolved alerts stay in the archive.
- Webhook deliveries retry on 5xx with exponential backoff and are tracked exactly-once via `alert_id:event_type:webhook_id` keys.
- Re-breach delivery order: `alert.resolved` for the old alert, then `alert.fired` for the new one.

## Tests

```bash
pytest
```
