"""POST /webhooks — register a plain JSON receiver."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.models import WebhookBody
from app.state import Webhook, state

router = APIRouter()


@router.post("/webhooks", status_code=status.HTTP_201_CREATED)
async def post_webhook(body: WebhookBody) -> dict:
    wh_id = f"wh-{uuid.uuid4().hex[:6]}"
    state.webhooks[wh_id] = Webhook(webhook_id=wh_id, url=body.url)
    return {"webhook_id": wh_id, "url": body.url}
