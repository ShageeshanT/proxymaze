# ProxyMaze Debug Handoff

Latest evaluator score: **~170/250**. Five attempts so far. The remaining failures are
all the same root-cause cluster: **outbound webhook POSTs from the dispatcher don't
arrive at the evaluator's capture server.** Probe classification, alert engine,
re-breach lifecycle — all working. The dispatcher needs network-level investigation.

This document is a complete brief for Terminal Claude to pick up and ship the next
fix without re-reading the chat history.

---

## 1. Where things stand

| Phase | Score | Notes |
|---|---|---|
| 1 — bootstrap | 10/10 ✓ | health, config |
| 2 — ingestion | 45/45 ✓ | pool CRUD + background polling |
| 3 — single failure | 30/30 ✓ | classification works after probe fix |
| 4 — threshold | **25/90** | 4.5 PASS, 4.6/4.6a/4.6b/4.7/4.8 FAIL — all webhook delivery |
| 5 — resolution | **10/20** | 5.3 PASS (state transitions), 5.4 FAIL (resolved webhook delivery) |
| 6 — re-breach | 25/30 | 6.3/6.4 PASS, one criterion partial |
| 7 — observability | 25/25 ✓ | metrics, DELETE, history |
| 8 — Slack/Discord bonus | **0/20** | 8.4 FAIL (Block Kit), 8.5 FAIL (Discord Embeds) |

**Diagnosis:** alerts ARE firing correctly (4.5 PASS proves the alert payload is
shaped right and visible via `GET /alerts`). The judge's capture server is just
never receiving the POST. Slack and Discord are also "delivered to a webhook URL"
checks — same delivery path, same failure.

---

## 2. Architecture summary (so you don't have to re-read the code)

- **`app/main.py`** — FastAPI app with lifespan that spawns two tasks:
  `prober_loop` and `dispatcher_loop`.
- **`app/state.py`** — module-level singleton `state` holds proxies, alerts,
  webhooks, integrations, `event_queue` (asyncio.Queue), `delivered_keys` set,
  `wake_event` (asyncio.Event), and lazy-init locks.
- **`app/prober.py`** — `prober_loop` runs every `check_interval_seconds`,
  re-reading config every tick and waking immediately on `state.wake_event`
  (POST/DELETE on /proxies sets it). Probes via `httpx.AsyncClient.get` with
  `follow_redirects=True` and the `_probe_one` rule (see below).
- **`app/alerts.py`** — `evaluate_alerts` holds `state.alert_lock`, fires when
  `failure_rate >= 0.20 and current_active_alert_id is None`, resolves when
  `failure_rate < 0.20 and current_active_alert_id is not None`. Enqueues
  `(type, payload)` dicts on `state.event_queue`.
- **`app/dispatcher.py`** — `dispatcher_loop` consumes from `event_queue`
  serially. For each event, builds targets (every webhook + matching
  integrations), POSTs to each via shared `httpx.AsyncClient` with retry
  schedule `RETRY_DELAYS = (1, 2, 4, 8)` and `REQUEST_TIMEOUT_S = 8.0`.
  Idempotency key: `f"{alert_id}:{event_type}:{receiver_id}"`.
- **`app/integrations.py`** — `format_slack` returns BOTH `attachments`
  (legacy) AND `blocks` (Block Kit). `format_discord` returns `embeds` with
  ISO `timestamp`.
- **`app/routes/`** — thin handlers, all push logic into state/alerts.

---

## 3. The current `_probe_one` rule (STRICT spec compliance)

```python
async def _probe_one(client, url, timeout_s):
    """Strict classification per spec:
    - 2xx response within request_timeout_ms ⇒ up
    - timeout, connection failure/refusal, or any 5xx ⇒ down
    - 4xx, 3xx-final, anything-else ⇒ down (not 2xx)

    Redirects are followed so the FINAL status is what's classified;
    we never override a 5xx with "alive" inferences.
    """
    try:
        r = await client.get(url, timeout=timeout_s,
                             follow_redirects=True, headers=_PROBE_HEADERS)
        return 200 <= r.status_code < 300
    except Exception:
        return False
```

`follow_redirects=True` is critical (judges' URLs return 301 → HTTPS).
**Do NOT change to `False`** — that drops Phase 3-7 to near-zero (verified
in Attempt 6). Strict 5xx-as-down is also correct (verified by Attempt 7
landing at ~170 pts). The "trust 3xx on 502" experiment was rolled back.

Regression tests in `tests/test_c_single_failure.py`:
- `test_b3_redirect_to_502_marks_down` — 301 → 502 must be `down`
- `test_b3_redirect_to_503_marks_down` — 301 → 503 must be `down`

---

## 4. The judges' URL pattern (verified via Railway SSH)

Probe target example:
```
GET http://evaluator.torchproxies.com/__mock/<MOCK_ID>/proxy/px-001
→ 301 Moved Permanently
  location: https://evaluator.torchproxies.com/__mock/<MOCK_ID>/proxy/px-001
GET https://evaluator.torchproxies.com/__mock/<MOCK_ID>/proxy/px-001
→ 502 Bad Gateway   (or 200 if "up", 503/504 if "down")
```

The 502 specifically means upstream broken (CDN can't reach origin) —
sometimes happens for healthy proxies too. That's why we trust the 3xx
when final is 502.

---

## 5. Score progression (so you understand what's already been tried)

| Attempt | Key change | Score |
|---|---|---|
| 3 | initial deploy, no redirect handling | 40 |
| 4 | `follow_redirects=True`, 2xx-only, wake event | (similar) |
| 5 | + Block Kit, Discord timestamp, retry budget = 1+2+4+8 | 170 |
| 6 | `follow_redirects=False`, 2xx OR 3xx as up — **WRONG** | 60 |
| 7 | `follow_redirects=True` + trust-3xx-on-502 hack — rolled back | (~170) |
| 8 (current) | reverted to strict spec: 2xx only = up, 5xx final = down | **~170** |

Lesson: the spec rule ("2xx = up, anything else = down") is what the evaluator
expects. Don't try to be clever with redirect classification. Phase 4/5/8
failures are NOT a probe issue — they're a webhook delivery issue.

---

## 6. The remaining problem in detail

**All five Phase 4 webhook tests, the Phase 5 resolved-delivery test, and BOTH
Phase 8 integration tests fail because the evaluator's capture server doesn't
record the POST.**

What we know:
- `POST /webhooks` and `POST /integrations` return 201. Webhook URL is
  recorded in `state.webhooks` / `state.integrations`.
- Alerts fire correctly (4.5 PASS confirms payload shape via `GET /alerts`).
- `state.metrics.webhook_deliveries` increments — proving `_deliver_one`
  gets a 2xx from SOMETHING. But evaluator never sees the POST.

Possible causes (rank-ordered by likelihood):

0. **MOST LIKELY: dispatcher POST follows a 301 and httpx converts POST→GET.**
   Per RFC 7231, on a 301/302 response, most HTTP clients downgrade POST to
   GET on the next request. httpx 0.28 follows this behavior. So if the
   judges register an `http://...` webhook URL that 301-redirects to
   `https://...`, our `client.post(url, json=body, follow_redirects=True)`
   sends:
   - POST http → 301
   - **GET** https (no body!)
   The evaluator never sees the POST it expects. The dispatcher records a
   2xx and marks delivered, but the actual test asserts "POST received"
   and finds nothing.
   - **Fix:** set `follow_redirects=False` in the dispatcher's
     `client.post` call (`app/dispatcher.py:77`). If the redirect IS
     valid (Slack/Discord webhooks), use 307/308 awareness or accept
     3xx as delivered (the evaluator would do its own follow then).
   - **Verify before fix:** SSH and test:
     ```python
     import httpx
     # Try a real evaluator webhook URL (find via logs)
     r = httpx.post("http://...", json={"x":1}, follow_redirects=True)
     # Look at: r.request.method (should be POST), r.history (should show
     # 301 with method change), r.text (does it confirm POST received?)
     ```

1. **The capture server URL also has the http→https→502 pattern, and our
   POST redirects to a broken HTTPS that returns 2xx-but-doesn't-record.**
   Equivalent diagnosis: the dispatcher uses `follow_redirects=True` and a
   redirect target lies about success.
   - **Test:** SSH in and `python -c "import httpx; r = httpx.post('<JUDGE_WEBHOOK_URL>',
     json={'test': 1}, follow_redirects=True); print(r.status_code, r.history, r.text[:200])"`
     (get the URL from `state.webhooks` via `GET /webhooks` — wait, that's not an
     endpoint; instead: temporarily add a `GET /__debug/webhooks` route that
     returns `state.webhooks`, deploy, fetch, then remove).
   - **Fix:** if the capture server is at `http://...` and redirects to a broken
     `https://...`, set `follow_redirects=False` ONLY for dispatcher (keep True
     for prober). POST to the URL the user gave us.

2. **Headers — the evaluator's capture server requires a specific header
   we're not sending** (e.g. `Content-Type: application/json` is sent, but
   maybe `X-Webhook-Source` or signature). Less likely; spec doesn't mention.

3. **Body shape — the evaluator validates the JSON strictly and rejects
   ours.** Already confirmed payload matches spec via `4.5 PASS`. But maybe
   some field type drift (float vs int, etc.).

4. **Connection-pool / async issue** — the dispatcher races and some
   POSTs are getting lost. Less likely (logs would show drops).

---

## 7. What to do first

```bash
# 1. SSH in and look at the most recent dispatcher logs.
railway link --project=7d1f016a-2ea1-4872-abc7-717ed7d92ff1 \
             --environment=2d754403-dce1-41cf-8102-7140eff2e34b \
             --service=decc6d39-64e7-4dbd-ac2a-09aa822b5604
railway logs --lines 5000 | grep -E "delivered|drop|dispatch|deliver|http" | tail -60

# 2. Specifically look for the webhook target URL — what host does the dispatcher
#    POST to? Does it go through the same evaluator.torchproxies.com pattern?
railway logs --lines 5000 | grep -E "POST http" | tail -20

# 3. If logs are empty for dispatcher (no events), the evaluator may register
#    webhooks AFTER firing the breach. Check the GET /alerts pattern in logs —
#    does the judge POST /webhooks before or after POST /proxies?

# 4. SSH and probe the capture URL manually:
railway ssh
cd /app
.venv/bin/python -c "
import httpx, json
url = '<paste capture URL from logs or temp endpoint>'
for follow in [True, False]:
    r = httpx.post(url, json={'event':'test','alert_id':'x'},
                   timeout=10, follow_redirects=follow)
    print(f'follow={follow}: status={r.status_code} hist={[h.status_code for h in r.history]}')
    print(f'   body: {r.text[:300]}')
"
```

---

## 8. If the diagnosis is "POST follows redirect and lands somewhere wrong"

Edit `app/dispatcher.py` line ~77, change `follow_redirects=True` to
`follow_redirects=False`, ship.

```python
r = await client.post(
    url,
    json=body,
    timeout=REQUEST_TIMEOUT_S,
    headers=DISPATCH_HEADERS,
    follow_redirects=False,   # CHANGED
)
```

Then handle 3xx as "delivered" if the evaluator wants it that way:

```python
if 200 <= r.status_code < 300:
    state.delivered_keys.add(key)
    ...
# Add this branch:
if 300 <= r.status_code < 400:
    # Capture server returned a redirect — treat as accepted.
    state.delivered_keys.add(key)
    state.metrics.webhook_deliveries += 1
    return
```

---

## 9. Local test discipline

Before any push:

```bash
cd C:\Users\shage\Desktop\proxymaze\proxymaze
python -m pytest -q -m "not slow"   # 128 tests, ~2 min
```

All 128 must pass. The slow stress tests (`test_stress.py`) are gated
behind the `slow` marker — run those occasionally with `pytest`.

---

## 10. File map

```
app/
├── main.py            entrypoint, lifespan, router mounts
├── state.py           dataclasses + singleton
├── models.py          pydantic bodies (all extra="ignore")
├── prober.py          probe loop with the trust-3xx-on-502 rule
├── alerts.py          fire/resolve, single-active invariant
├── dispatcher.py      FIFO consumer, retries, exactly-once
├── integrations.py    Slack (attachments + Block Kit) + Discord embeds
├── utils.py           extract_proxy_id, utcnow_iso
└── routes/            thin route handlers
tests/
├── conftest.py        autouse state.reset, fast retry monkey-patch, fixtures
├── helpers/mock_servers.py    MockProxyServer + MockReceiver in threads
├── test_a_bootstrap.py
├── test_b_ingestion.py
├── test_c_single_failure.py   ← contains redirect classification regression tests
├── test_d_threshold_alerts.py
├── test_e_resolution.py
├── test_f_rebreach.py
├── test_g_observability.py
├── test_h_slack.py
├── test_i_discord.py
├── test_z_edge_cases.py
└── test_stress.py     marked @slow
Procfile               web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
requirements.txt       pinned versions
```

---

## 11. Railway connection

```bash
railway link --project=7d1f016a-2ea1-4872-abc7-717ed7d92ff1 \
             --environment=2d754403-dce1-41cf-8102-7140eff2e34b \
             --service=decc6d39-64e7-4dbd-ac2a-09aa822b5604

# Logs (last 5000 lines, no streaming)
railway logs --lines 5000

# SSH into running container
railway ssh
# inside: /app is the working directory, /app/.venv/bin/python is the interpreter,
# curl is NOT installed — use python httpx for outbound checks.
```

User's GitHub: ShageeshanT. Push triggers Railway redeploy in ~60s.

---

## 12. Don't break what's working

These behaviors are ALL passing in the live evaluator and must stay:

- Phase 1: `/health`, `/config` get/post, defaults `15` and `3000`.
- Phase 2: pool CRUD, replace semantics, alert preservation, background
  polling within ~5s of POST.
- Phase 3: 5xx → down, timeout → down, recovery → up, single failure detected.
- Phase 7: `/metrics`, `DELETE /proxies` preserves alerts, history JSON array.

If you're tempted to make a change in `prober.py`, `alerts.py`, or any route
handler, FIRST run the relevant test file. The probe rule especially is fragile.

Good luck. The fix is probably one POST-side change in `dispatcher.py`.
