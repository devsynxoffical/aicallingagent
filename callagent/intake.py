"""Requests that reach the business through its website's forms.

The website hands each submission to ``POST /api/intake/requests`` with the intake
token, a credential that can do only that. People then work the list in the console.
"""

from __future__ import annotations

import hmac
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .apideps import acting_as, organization_id, require_api_token
from .config import Settings, get_settings
from .db import WebsiteRequest, default_organization_id, run_db, utcnow

intake_router = APIRouter(prefix="/api/intake", tags=["website requests"])
requests_router = APIRouter(prefix="/api/requests", dependencies=[Depends(require_api_token)], tags=["website requests"])

KINDS = ("demo", "trial", "contact", "newsletter", "callback")


class RequestIn(BaseModel):
    kind: Literal["demo", "trial", "contact", "newsletter", "callback"]
    email: str | None = Field(default=None, max_length=254)
    name: str | None = Field(default=None, max_length=120)
    company: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=32, pattern=r"^[+()\d\s-]*$")
    team_size: str | None = Field(default=None, max_length=16)
    message: str | None = Field(default=None, max_length=2000)


class HandledIn(BaseModel):
    handled: bool = True


def require_intake_token(authorization: str | None = Header(default=None), settings: Settings = Depends(get_settings)) -> None:
    given = authorization.split(" ", 1)[1].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    expected = settings.intake_token or ""
    # The same answer whether intake is switched off or the token is wrong.
    if not given or not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="missing or invalid intake token")


def describe(r: WebsiteRequest) -> dict:
    return {
        "id": r.id,
        "kind": r.kind,
        "email": r.email,
        "name": r.name,
        "company": r.company,
        "phone": r.phone,
        "team_size": r.team_size,
        "message": r.message,
        "status": r.status,
        "handled_by": r.handled_by,
        "handled_at": r.handled_at.isoformat() if r.handled_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@intake_router.post("/requests", status_code=201, dependencies=[Depends(require_intake_token)])
async def receive_request(body: RequestIn):
    """Store one form submission. A request needs a way to answer it: an email, or a phone number to call back."""
    email = (body.email or "").strip().lower() or None
    phone = (body.phone or "").strip() or None
    if body.kind == "callback" and not phone:
        raise HTTPException(422, "a callback request needs a phone number")
    if body.kind != "callback" and not email:
        raise HTTPException(422, "this request needs an email address")

    def _save(s):
        row = WebsiteRequest(
            organization_id=default_organization_id(s), kind=body.kind, email=email, phone=phone,
            name=(body.name or "").strip() or None, company=(body.company or "").strip() or None,
            team_size=(body.team_size or "").strip() or None, message=(body.message or "").strip() or None,
        )
        s.add(row)
        s.flush()
        return {"id": row.id}

    return await run_db(_save)


@requests_router.get("")
async def list_requests(status: str | None = None, kind: str | None = None, limit: int = 50, offset: int = 0):
    def _q(s):
        org = organization_id()
        query = select(WebsiteRequest).where(WebsiteRequest.organization_id == org)
        if status in ("new", "handled"):
            query = query.where(WebsiteRequest.status == status)
        if kind in KINDS:
            query = query.where(WebsiteRequest.kind == kind)
        total = s.execute(select(func.count()).select_from(query.subquery())).scalar_one()
        waiting = s.execute(select(func.count()).select_from(WebsiteRequest).where(WebsiteRequest.organization_id == org, WebsiteRequest.status == "new")).scalar_one()
        rows = s.execute(query.order_by(WebsiteRequest.id.desc()).limit(max(1, min(limit, 200))).offset(max(0, offset))).scalars()
        return {"total": total, "new": waiting, "requests": [describe(r) for r in rows]}

    return await run_db(_q)


@requests_router.post("/{request_id}/handled")
async def mark_handled(request_id: int, request: Request, body: HandledIn | None = None):
    """Record that someone dealt with the request, or put it back in the queue."""
    handled = (body or HandledIn()).handled
    who = acting_as(request, "api token")

    def _mark(s):
        row = s.get(WebsiteRequest, request_id)
        if row is None or row.organization_id != organization_id():
            raise HTTPException(404, "request not found")
        row.status = "handled" if handled else "new"
        row.handled_by = who if handled else None
        row.handled_at = utcnow() if handled else None
        s.flush()
        return describe(row)

    return await run_db(_mark)
