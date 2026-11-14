"""Dependencies shared by every /api router."""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select

from . import accounts
from .config import Settings, get_settings, resolve_api_token
from .db import Campaign, current_organization, default_organization_id, known_default_organization, run_db

# The deployment's operator names the organization to act in with this header. It is
# honoured for the engine API token only; from anyone else it is ignored.
ORGANIZATION_HEADER = "X-DevyTalk-Organization"


async def require_api_token(
    request: Request,
    authorization: str | None = Header(default=None),
    acting_in: str | None = Header(default=None, alias=ORGANIZATION_HEADER),
    settings: Settings = Depends(get_settings),
) -> accounts.Principal:
    """Every /api route needs ``Authorization: Bearer <token>``: the engine's API token
    or the session token of a signed-in person. This decides three things: who is
    calling, whether their role allows the route (``accounts.required_role``), and
    which organization's data the request may touch. Twilio routes use signature
    validation instead."""
    given = authorization.split(" ", 1)[1].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not given:
        raise HTTPException(status_code=401, detail="sign in, or send the API token (see DATA_DIR/api_token or API_TOKEN)")
    if hmac.compare_digest(given.encode(), resolve_api_token(settings).encode()):

        def _operator(s):
            if not acting_in:
                return accounts.operator_principal(default_organization_id(s))
            org = accounts.find_organization(s, acting_in)
            return accounts.operator_principal(org.id) if org is not None else None

        known = None if acting_in else known_default_organization()
        principal = accounts.operator_principal(known) if known is not None else await run_db(_operator)
        if principal is None:
            raise HTTPException(status_code=404, detail="no such organization")
    elif given.startswith(accounts.KEY_PREFIX):
        principal = await run_db(lambda s: accounts.principal_for_api_key(s, given), unrestricted=True)
        if principal is None:
            raise HTTPException(status_code=401, detail="this API key is unknown or was revoked")
    else:
        principal = await run_db(lambda s: accounts.principal_for_session(s, given))
        if principal is None:
            raise HTTPException(status_code=401, detail="missing or invalid API token, or the session has ended")
    # Known from here on, whether or not they are allowed: a refused attempt is recorded with who made it.
    request.state.caller = principal
    route = getattr(request.scope.get("route"), "path", request.url.path)
    needed = accounts.required_role(request.method, route)
    if principal.kind == "key" and route.startswith(accounts.KEY_FORBIDDEN_ROUTES):
        raise HTTPException(status_code=403, detail="an API key cannot manage people, passwords or keys; sign in as a person for that")
    if not accounts.allows(principal, needed):
        if needed == accounts.PLATFORM:
            raise HTTPException(status_code=403, detail="only the operator of this deployment can manage organizations")
        raise HTTPException(status_code=403, detail=f"your role ({principal.role}) cannot do this; it needs the {needed} role")
    request.state.principal = principal
    # From here on, every lookup made for this request is confined to this organization.
    current_organization.set(principal.organization_id)
    return principal


def current_principal(request: Request) -> accounts.Principal:
    """Who is making this request. Only valid on routes that depend on ``require_api_token``."""
    return request.state.principal


def organization_id() -> int:
    """The organization of the request being served. Fails closed: code that asks
    outside an authenticated request gets an error, never somebody's data."""
    org = current_organization.get()
    if org is None:
        raise RuntimeError("no organization in context: this lookup is only valid inside an authenticated request")
    return org


def acting_as(request: Request, given: str = "") -> str:
    """Who to record as having taken an action: the signed-in person, whatever the
    request body claims; for the API token, the name the caller gave."""
    principal = getattr(request.state, "principal", None)
    if principal is not None and principal.kind in ("user", "key"):
        return principal.email
    return given


def get_campaign(s, name: str) -> Campaign:
    """The caller's campaign with this name. A campaign of another organization is
    reported exactly like one that does not exist."""
    c = s.execute(select(Campaign).where(Campaign.organization_id == organization_id(), Campaign.name == name)).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, f"campaign '{name}' not found")
    return c
