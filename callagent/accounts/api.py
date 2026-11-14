"""HTTP API for signing in and for managing the people who may."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import accounts
from ..apideps import acting_as, current_principal, require_api_token
from . import invitations
from .. import orgsettings
from ..config import get_settings
from ..db import Organization, User, run_db

public = APIRouter(prefix="/api/auth", tags=["accounts"])
router = APIRouter(prefix="/api", dependencies=[Depends(require_api_token)], tags=["accounts"])


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=accounts.MAX_PASSWORD_LENGTH)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=accounts.MAX_PASSWORD_LENGTH)
    new_password: str = Field(max_length=accounts.MAX_PASSWORD_LENGTH)


class UserIn(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(default="", max_length=200)
    role: str = "viewer"
    password: str = Field(max_length=accounts.MAX_PASSWORD_LENGTH)


class UserPatch(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: str | None = None
    active: bool | None = None
    password: str | None = Field(default=None, max_length=accounts.MAX_PASSWORD_LENGTH)


class OrganizationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    admin_email: str = Field(max_length=254)
    admin_name: str = Field(default="", max_length=200)
    admin_password: str | None = Field(default=None, max_length=accounts.MAX_PASSWORD_LENGTH,
                                       description="Leave out to get an invitation link instead: the admin then chooses their own password.")


class InvitationIn(BaseModel):
    email: str = Field(max_length=254)
    name: str = Field(default="", max_length=200)
    role: str = "viewer"


class AcceptIn(BaseModel):
    password: str = Field(max_length=accounts.MAX_PASSWORD_LENGTH)
    name: str | None = Field(default=None, max_length=200)


def _describe_principal(p: accounts.Principal, organization: str) -> dict:
    return {
        "kind": p.kind, "id": p.user_id, "email": p.email, "name": p.name, "role": p.role,
        "organization": {"id": p.organization_id, "name": organization},
        # True for the deployment's operator, who can create organizations.
        "platform": p.platform,
    }


@public.post("/login")
async def login(body: LoginIn):
    """Sign in with an email and a password. The token returned is the session: send it
    as ``Authorization: Bearer`` on later requests."""
    result = await run_db(lambda s: accounts.login(s, body.email, body.password))
    if not result.ok:
        raise HTTPException(result.status, result.error)
    return {"token": result.token, "expires_at": result.expires_at, "user": result.user}


@router.get("/auth/me")
async def me(request: Request):
    principal = current_principal(request)
    name = await run_db(lambda s: getattr(s.get(Organization, principal.organization_id), "name", ""))
    return _describe_principal(principal, name)


@router.post("/auth/logout")
async def logout(request: Request):
    principal = current_principal(request)
    await run_db(lambda s: accounts.logout(s, principal))
    return {"ok": True}


@router.post("/auth/password")
async def change_password(body: PasswordIn, request: Request):
    """Change your own password. Your other sign-ins are ended."""
    principal = current_principal(request)
    if principal.kind != "user":
        raise HTTPException(400, "the API token has no password; change API_TOKEN on the engine instead")

    def _change(s):
        user = s.get(User, principal.user_id)
        if user is None or not accounts.verify_password(body.current_password, user.password_hash):
            return "the current password is incorrect"
        try:
            accounts.set_password(s, user, body.new_password, keep_session=principal.session_id)
        except accounts.AccountError as e:
            return str(e)
        return None

    problem = await run_db(_change)
    if problem:
        raise HTTPException(400, problem)
    return {"ok": True}


@router.get("/users")
async def list_users(request: Request):
    """The people of the caller's organization."""
    org = current_principal(request).organization_id
    return await run_db(lambda s: [accounts.describe(u) for u in accounts.list_users(s, org)])


@router.post("/users", status_code=201)
async def create_user(body: UserIn, request: Request):
    """Add a person to the caller's organization. Which organization is never taken from the request."""
    org = current_principal(request).organization_id

    def _create(s):
        try:
            return accounts.describe(accounts.create_user(s, body.email, body.password, name=body.name, role=body.role, organization_id=org))
        except accounts.AccountError as e:
            raise HTTPException(409 if "already has an account" in str(e) else 400, str(e)) from None

    return await run_db(_create)


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UserPatch, request: Request):
    """Rename, change the role, deactivate or reactivate, or set a new password. A
    deactivated account, or one whose password was reset, is signed out everywhere."""
    principal = current_principal(request)

    def _update(s):
        user = accounts.get_user(s, user_id, principal.organization_id)
        if user is None:
            raise HTTPException(404, "user not found")
        try:
            return accounts.describe(accounts.update_user(s, user, actor=principal, name=body.name, role=body.role, active=body.active, password=body.password))
        except accounts.AccountError as e:
            raise HTTPException(400, str(e)) from None

    return await run_db(_update)


@router.post("/users/{user_id}/reset-link", status_code=201)
async def create_reset_link(user_id: int, request: Request):
    """A one-time link by which this person chooses a new password. The answer carries
    its token, this once; hand it to them yourself. It works for a day."""
    principal = current_principal(request)

    def _link(s):
        user = accounts.get_user(s, user_id, principal.organization_id)
        if user is None:
            raise HTTPException(404, "user not found")
        try:
            invitation, token = invitations.reset_link(s, user, by=acting_as(request, "api token"))
        except accounts.AccountError as e:
            raise HTTPException(400, str(e)) from None
        return {**invitations.describe(invitation), "token": token}

    return await run_db(_link)


# ----------------------------------------------------------------------------- invitations


@router.get("/invitations")
async def list_invitations(request: Request):
    """Invitations to join the caller's organization, newest first. Tokens are never listed."""
    org = current_principal(request).organization_id
    return await run_db(lambda s: [invitations.describe(i) for i in invitations.list_invitations(s, org)])


@router.post("/invitations", status_code=201)
async def create_invitation(body: InvitationIn, request: Request):
    """Invite a person to the caller's organization. The answer carries the link's token,
    this once: hand the link to them yourself. They choose their own password. It works for a week."""
    org = current_principal(request).organization_id

    def _invite(s):
        try:
            invitation, token = invitations.invite(s, org, body.email, name=body.name, role=body.role, by=acting_as(request, "api token"))
        except accounts.AccountError as e:
            raise HTTPException(409 if "already has an account" in str(e) else 400, str(e)) from None
        return {**invitations.describe(invitation), "token": token}

    return await run_db(_invite)


@router.delete("/invitations/{invitation_id}")
async def withdraw_invitation(invitation_id: int, request: Request):
    """Withdraw an invitation nobody has accepted yet. Its link stops working at once."""
    org = current_principal(request).organization_id

    def _withdraw(s):
        try:
            invitation = invitations.withdraw(s, org, invitation_id)
        except accounts.AccountError as e:
            raise HTTPException(400, str(e)) from None
        if invitation is None:
            raise HTTPException(404, "no such invitation")
        return invitations.describe(invitation)

    return await run_db(_withdraw)


@public.get("/invitations/{token}")
async def read_invitation(token: str):
    """What a one-time link is for, shown to whoever holds it before they choose a
    password. An unknown, used, withdrawn or expired link is simply not found."""
    found = await run_db(lambda s: invitations.preview(s, token), unrestricted=True)
    if found is None:
        raise HTTPException(404, invitations.GONE)
    return found


@public.post("/invitations/{token}")
async def accept_invitation(token: str, body: AcceptIn):
    """Use a one-time link: choose a password, and be signed in. The link stops working."""
    result = await run_db(lambda s: invitations.accept(s, token, body.password, name=body.name), unrestricted=True)
    if not result.ok:
        raise HTTPException(result.status, result.error)
    return {"token": result.token, "expires_at": result.expires_at, "user": result.user}


# ----------------------------------------------------------------------------- organizations (the deployment's operator only)


@router.get("/organizations")
async def list_organizations():
    return await run_db(lambda s: [accounts.describe_organization(s, o) for o in accounts.list_organizations(s)], unrestricted=True)


@router.post("/organizations", status_code=201)
async def create_organization(body: OrganizationIn, request: Request):
    """A new customer organization and its first admin, who is created with the password
    given, or, without one, invited by a link. The admin then adds everyone else."""

    def _create(s):
        try:
            if body.admin_password is None:
                # Nobody but the admin ever knows their password: they get a link and choose it.
                org = accounts.create_empty_organization(s, body.name)
                invitation, token = invitations.invite(s, org.id, body.admin_email, name=body.admin_name, role="admin", by=acting_as(request, "api token"))
                return {**accounts.describe_organization(s, org), "admin": None, "invitation": {**invitations.describe(invitation), "token": token}}
            org, admin = accounts.create_organization(s, body.name, admin_email=body.admin_email, admin_password=body.admin_password, admin_name=body.admin_name)
        except accounts.AccountError as e:
            raise HTTPException(409 if "already" in str(e) else 400, str(e)) from None
        return {**accounts.describe_organization(s, org), "admin": accounts.describe(admin)}

    return await run_db(_create, unrestricted=True)


class TelephonyIn(BaseModel):
    from_numbers: list[str] | None = Field(default=None, description="Every number this organization may call from, in international form. Replaces the list.")
    cost_per_connected_minute_usd: float | None = Field(default=None, ge=0)
    cost_per_call_attempt_usd: float | None = Field(default=None, ge=0)
    clear_rates: bool = Field(default=False, description="Go back to the deployment's rates.")


@router.put("/organizations/{organization_id}/telephony")
async def set_organization_telephony(organization_id: int, body: TelephonyIn):
    """Assign an organization the numbers it calls from, and its rates. For the
    deployment's operator: a caller number must be one the operator holds at the phone
    provider, so it is not for the organization to type in."""

    def _set(s):
        org = s.get(Organization, organization_id)
        if org is None:
            raise HTTPException(404, "no such organization")
        try:
            orgsettings.set_by_operator(s, org, from_numbers=body.from_numbers, cost_per_connected_minute_usd=body.cost_per_connected_minute_usd,
                                        cost_per_call_attempt_usd=body.cost_per_call_attempt_usd, clear_rates=body.clear_rates)
        except orgsettings.SettingsError as e:
            raise HTTPException(409 if "already assigned" in str(e) else 400, str(e)) from None
        return {**accounts.describe_organization(s, org), "settings": orgsettings.describe(org, get_settings())}

    return await run_db(_set, unrestricted=True)


# ----------------------------------------------------------------------------- an organization's own settings


class OrganizationSettingsIn(BaseModel):
    human_transfer_number: str | None = Field(default=None, description="Where a call is handed to a person. Empty: the agent cannot transfer.")
    booking_webhook_url: str | None = Field(default=None, description="Where booked meetings are reported. https only. Empty: nowhere.")
    booking_webhook_secret: str | None = Field(default=None, description="Signs each report. Leave out to keep the one already set; it is never shown.")
    retention_days: int | None = Field(default=None, ge=1, le=3650, description="Remove what was said on a call this many days after it ended. Leave out to keep the current setting; null to keep everything.")


@router.get("/organization/settings")
async def get_organization_settings(request: Request):
    """What is this organization's own: its caller numbers, transfer number, booking webhook and rates."""
    principal = current_principal(request)
    return await run_db(lambda s: orgsettings.describe(s.get(Organization, principal.organization_id), get_settings()))


@router.put("/organization/settings")
async def set_organization_settings(body: OrganizationSettingsIn, request: Request):
    """Set where calls are transferred and where bookings are reported. The caller
    numbers and the rates are assigned by the deployment's operator."""
    principal = current_principal(request)

    def _set(s):
        org = s.get(Organization, principal.organization_id)
        try:
            orgsettings.set_by_admin(org, human_transfer_number=body.human_transfer_number, booking_webhook_url=body.booking_webhook_url,
                                     booking_webhook_secret=body.booking_webhook_secret, keep_secret="booking_webhook_secret" not in body.model_fields_set,
                                     retention_days=body.retention_days, keep_retention="retention_days" not in body.model_fields_set)
        except orgsettings.SettingsError as e:
            raise HTTPException(400, str(e)) from None
        return orgsettings.describe(org, get_settings())

    return await run_db(_set)


# ----------------------------------------------------------------------------- API keys


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="What will use it, so it can be recognised later.")
    role: str = Field(default="viewer", description="viewer, operator or admin: what the key may do.")


@router.get("/keys")
async def list_api_keys(request: Request):
    principal = current_principal(request)
    return await run_db(lambda s: [accounts.describe_api_key(k) for k in accounts.list_api_keys(s, principal.organization_id)])


@router.post("/keys", status_code=201)
async def create_api_key(body: ApiKeyIn, request: Request):
    """Create a key for this organization's own systems. The key is in the answer and
    is not stored: copy it now."""
    principal = current_principal(request)

    def _create(s):
        try:
            record, key = accounts.create_api_key(s, principal.organization_id, body.name, body.role, principal.email or "api token")
        except accounts.AccountError as e:
            raise HTTPException(400, str(e)) from None
        return {**accounts.describe_api_key(record), "key": key}

    return await run_db(_create)


@router.delete("/keys/{key_id}")
async def revoke_api_key(key_id: int, request: Request):
    """Revoke a key. It stops working at once and cannot be restored."""
    principal = current_principal(request)

    def _revoke(s):
        record = accounts.revoke_api_key(s, principal.organization_id, key_id)
        if record is None:
            raise HTTPException(404, "no such key")
        return accounts.describe_api_key(record)

    return await run_db(_revoke)

