"""HTTP API for signing in and for managing the people who may."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import accounts
from ..apideps import current_principal, require_api_token
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
    admin_password: str = Field(max_length=accounts.MAX_PASSWORD_LENGTH)


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


# ----------------------------------------------------------------------------- organizations (the deployment's operator only)


@router.get("/organizations")
async def list_organizations():
    return await run_db(lambda s: [accounts.describe_organization(s, o) for o in accounts.list_organizations(s)], unrestricted=True)


@router.post("/organizations", status_code=201)
async def create_organization(body: OrganizationIn):
    """A new customer organization and its first admin. The admin then adds everyone else."""

    def _create(s):
        try:
            org, admin = accounts.create_organization(s, body.name, admin_email=body.admin_email, admin_password=body.admin_password, admin_name=body.admin_name)
        except accounts.AccountError as e:
            raise HTTPException(409 if "already" in str(e) else 400, str(e)) from None
        return {**accounts.describe_organization(s, org), "admin": accounts.describe(admin)}

    return await run_db(_create, unrestricted=True)
