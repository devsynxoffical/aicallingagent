"""Dependencies shared by every /api router."""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request

from . import accounts
from .config import Settings, get_settings, resolve_api_token
from .db import Campaign, get_campaign_by_name, run_db


async def require_api_token(request: Request, authorization: str | None = Header(default=None), settings: Settings = Depends(get_settings)) -> accounts.Principal:
    """Every /api route needs ``Authorization: Bearer <token>``: the engine's API token
    or the session token of a signed-in person. The caller's role must allow the route
    (see ``accounts.required_role``). Twilio routes use signature validation instead."""
    given = authorization.split(" ", 1)[1].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not given:
        raise HTTPException(status_code=401, detail="sign in, or send the API token (see DATA_DIR/api_token or API_TOKEN)")
    if hmac.compare_digest(given.encode(), resolve_api_token(settings).encode()):
        principal = accounts.API_TOKEN_PRINCIPAL
    else:
        principal = await run_db(lambda s: accounts.principal_for_session(s, given))
        if principal is None:
            raise HTTPException(status_code=401, detail="missing or invalid API token, or the session has ended")
    route = getattr(request.scope.get("route"), "path", request.url.path)
    needed = accounts.required_role(request.method, route)
    if not accounts.allows(principal.role, needed):
        raise HTTPException(status_code=403, detail=f"your role ({principal.role}) cannot do this; it needs the {needed} role")
    request.state.principal = principal
    return principal


def current_principal(request: Request) -> accounts.Principal:
    """Who is making this request. Only valid on routes that depend on ``require_api_token``."""
    return request.state.principal


def acting_as(request: Request, given: str = "") -> str:
    """Who to record as having taken an action: the signed-in person, whatever the
    request body claims; for the API token, the name the caller gave."""
    principal = getattr(request.state, "principal", None)
    if principal is not None and principal.kind == "user":
        return principal.email
    return given


def get_campaign(s, name_or_id: str) -> Campaign:
    c = s.get(Campaign, int(name_or_id)) if name_or_id.isdigit() else get_campaign_by_name(s, name_or_id)
    if c is None:
        raise HTTPException(404, f"campaign '{name_or_id}' not found")
    return c
