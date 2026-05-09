"""POST /integrations — register a Slack or Discord receiver."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.models import IntegrationBody
from app.state import Integration, state

router = APIRouter()


@router.post("/integrations", status_code=status.HTTP_201_CREATED)
async def post_integration(body: IntegrationBody) -> dict:
    integ_id = f"int-{uuid.uuid4().hex[:6]}"
    integ = Integration(
        integration_id=integ_id,
        type=body.type,
        webhook_url=body.webhook_url,
        username=body.username,
        events=list(body.events),
    )
    state.integrations.append(integ)
    return {
        "integration_id": integ_id,
        "type": integ.type,
        "webhook_url": integ.webhook_url,
        "username": integ.username,
        "events": integ.events,
    }
