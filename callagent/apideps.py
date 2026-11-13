"""Dependencies shared by every /api router."""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException

from .config import Settings, get_settings, resolve_api_token
from .db import Campaign, get_campaign_by_name


async def require_api_token(authorization: str | None = Header(default=None), settings: Settings = Depends(get_settings)):
    """Every /api route needs `Authorization: Bearer <token>`. Twilio routes use signature validation instead."""
    expected = resolve_api_token(settings)
    given = authorization.split(" ", 1)[1].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not given or not secrets.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="missing or invalid API token (see DATA_DIR/api_token or API_TOKEN)")


def get_campaign(s, name_or_id: str) -> Campaign:
    c = s.get(Campaign, int(name_or_id)) if name_or_id.isdigit() else get_campaign_by_name(s, name_or_id)
    if c is None:
        raise HTTPException(404, f"campaign '{name_or_id}' not found")
    return c
