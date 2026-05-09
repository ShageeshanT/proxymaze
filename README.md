# ProxyMaze

In-memory HTTP API service that monitors a pool of proxy URLs, fires alerts when the pool failure rate crosses a 20% threshold, and delivers webhook notifications (plain JSON, Slack, or Discord) with retries and exactly-once semantics. Built on FastAPI + httpx + asyncio; state lives in memory (no external dependencies).

## Architecture

```
                ┌─────────────────────────────────────────────┐
                │                FastAPI app                  │
                │  routes/                                    │
                │   ├── health.py       /health, /            │
                │   ├── config.py       GET/POST /config      │
                │   ├── proxies.py      POST/GET/DELETE /…    │
                │   ├── alerts.py       GET /alerts           │
                │   ├── webhooks.py     POST /webhooks        │
                │   ├── integrations.py POST /integrations    │
                │   └── metrics.py      GET /metrics          │
                └────────┬───────────────────────────┬────────┘
                         │                           │
                  read / mutate                  read / enqueue
                         │                           │
                         ▼                           ▼
            ┌───────────────────────┐    ┌─────────────────────┐
            │   state.py (in-mem)   │◄───┤  alerts.py          │
            │  proxies / alerts /   │    │  fire / resolve     │
            │  webhooks / metrics / │    │  (single active)    │
            │  delivered_keys       │    └──────────┬──────────┘
            └──────────┬────────────┘               │ enqueue events
                       │                            ▼
        every cycle:   │                   ┌─────────────────────┐
            ┌──────────▼─────────┐         │  event_queue (FIFO) │
            │  prober.py         │         └──────────┬──────────┘
            │  asyncio.gather of │                    │
            │  probe_one(url)    │                    ▼
            │  follows redirects │         ┌─────────────────────┐
            └────────────────────┘         │  dispatcher.py      │
                                           │  retries on 5xx     │
                                           │  exactly-once via   │
                                           │  delivered_keys set │
                                           └──────────┬──────────┘
                                                      │ POST JSON
                                                      ▼
                                  webhook  /  Slack  /  Discord
```

Two background tasks run from the FastAPI lifespan: the **prober** loop (probes every proxy in parallel each `check_interval_seconds`, re-reads config every tick, wakes immediately when the pool changes) and the **dispatcher** loop (drains the FIFO event queue, fans out to every receiver, retries on 5xx with exponential backoff, deduplicates with `alert_id:event_type:receiver_id` keys).

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` for a service descriptor, `/health` for liveness.

## Deploy to Railway / Render / Fly.io

1. Push the repo to GitHub.
2. Create a new Web Service from the repo.
3. The included `Procfile` (`web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`) is auto-detected. No further config needed.
4. After deploy, hit `https://<your-service>/health` to confirm.

`render.yaml` is provided as a Render blueprint; Railway/Fly read the `Procfile` directly.

## Endpoints

### Bootstrap

```bash
curl https://<host>/health
# {"status":"ok"}

curl https://<host>/config
# {"check_interval_seconds":15,"request_timeout_ms":3000}

curl -X POST https://<host>/config -H 'content-type: application/json' \
  -d '{"check_interval_seconds":5,"request_timeout_ms":2000}'
```

### Proxy pool

```bash
# Add proxies (replace=true clears first; replace=false appends).
curl -X POST https://<host>/proxies -H 'content-type: application/json' -d '{
  "proxies": ["https://capture.example.com/proxy/px-101",
              "https://capture.example.com/proxy/px-102"],
  "replace": true
}'

# Pool summary (live status, total, up, down, failure_rate)
curl https://<host>/proxies

# One proxy with full history
curl https://<host>/proxies/px-101
curl https://<host>/proxies/px-101/history

# Clear the pool. Alert archive is preserved.
curl -X DELETE https://<host>/proxies
```

### Alerts and webhooks

```bash
# All alerts (active + resolved). Active alert's failed_proxy_ids reflect
# the live pool; resolved alerts return the snapshot frozen at resolve time.
curl https://<host>/alerts

# Register a plain JSON webhook receiver.
curl -X POST https://<host>/webhooks -H 'content-type: application/json' \
  -d '{"url":"https://receiver.example/hook"}'

# Register a Slack or Discord integration.
curl -X POST https://<host>/integrations -H 'content-type: application/json' -d '{
  "type":"slack",
  "webhook_url":"https://hooks.slack.com/services/AAA/BBB/CCC",
  "username":"ProxyWatch",
  "events":["alert.fired","alert.resolved"]
}'

curl -X POST https://<host>/integrations -H 'content-type: application/json' -d '{
  "type":"discord",
  "webhook_url":"https://discord.com/api/webhooks/.../..."
}'
```

### Observability

```bash
curl https://<host>/metrics
# {"total_checks":120,"current_pool_size":10,"active_alerts":1,"total_alerts":3,"webhook_deliveries":4}
```

## Behavior contract

- **Probe classification** — a 2xx (after following redirects) within `request_timeout_ms` is `up`; everything else (4xx, 5xx, timeout, connection error) is `down`.
- **Threshold** — fixed at 0.20. Alert fires when `down/total >= 0.20` and resolves when below.
- **Single active alert** — at any moment at most one alert has `status: "active"`. Persistent breaches do not re-fire.
- **Re-breach** — a new breach after resolve mints a brand-new `alert_id`; the resolved alert stays in the archive unchanged.
- **Event order on receivers** — re-breach delivery sequence is `alert.resolved (old)` then `alert.fired (new)`.
- **Exactly-once delivery** — tracked by `alert_id:event_type:receiver_id` key; retries on 5xx use exponential backoff (1s, 2s, 4s, 8s, 16s, 32s, 60s, then drop). 4xx is non-retryable.
- **Live consistency** — `GET /proxies`, `GET /alerts`, and the most recent webhook payload all agree on `failed_proxy_ids`, `failed_proxies`, `total_proxies`, and `threshold`.
- **All bodies tolerate unknown fields**.
- **All timestamps** are ISO 8601 UTC with a literal `Z` suffix (never `+00:00`).

## Tests

```bash
pytest -v
```

The suite (`tests/`) covers all spec invariants plus edge cases — bootstrap, ingestion, single-failure modes, threshold behavior, resolution, re-breach lifecycle, observability, Slack/Discord shape, and concurrency races.

## Limitations

- State is in-memory: a process restart drops all proxies, alerts, webhooks. Acceptable for the evaluation use case (judges spin up cold).
- Webhook delivery is best-effort once the retry budget is exhausted; persistent receiver failures are logged and dropped.
