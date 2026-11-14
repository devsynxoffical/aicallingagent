"""Joining an organization, and getting back into an account, by a one-time link.

An admin makes a link for a person and hands it over themselves (this engine sends no
email). Whoever opens it chooses their own password, so an admin never knows anyone
else's. A link works once, for a limited time, and can be withdrawn.

The link's token is the whole credential: 256 random bits, of which only a SHA-256 is
stored. An unknown, used, withdrawn and expired link all get the same answer.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Invitation, Organization, User, utcnow
from . import ROLES, AccountError, LoginResult, check_password_rules, create_user, get_user_by_email, normalize_email, open_session, set_password

TOKEN_PREFIX = "dti_"
JOIN_DAYS = 7
RESET_HOURS = 24
MAX_PENDING = 50
GONE = "this link is not valid any more. Ask for a new one."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def state(invitation: Invitation) -> str:
    if invitation.used_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "withdrawn"
    return "expired" if invitation.expires_at <= utcnow() else "pending"


def describe(invitation: Invitation) -> dict[str, Any]:
    return {
        "id": invitation.id, "purpose": invitation.purpose, "email": invitation.email, "name": invitation.name, "role": invitation.role, "state": state(invitation),
        "created_by": invitation.created_by, "created_at": invitation.created_at.isoformat() if invitation.created_at else None,
        "expires_at": invitation.expires_at.isoformat() if invitation.expires_at else None, "used_at": invitation.used_at.isoformat() if invitation.used_at else None,
    }


def _pending(s: Session, organization_id: int, **match: Any) -> list[Invitation]:
    query = select(Invitation).where(Invitation.organization_id == organization_id, Invitation.used_at.is_(None), Invitation.revoked_at.is_(None), Invitation.expires_at > utcnow())
    return list(s.execute(query.filter_by(**match)).scalars())


def _issue(s: Session, **values: Any) -> tuple[Invitation, str]:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    invitation = Invitation(token_hash=_hash(token), **values)
    s.add(invitation)
    s.flush()
    return invitation, token


def invite(s: Session, organization_id: int, email: str, *, name: str = "", role: str = "viewer", by: str = "") -> tuple[Invitation, str]:
    """A link for someone to join the organization with this role. Returns the
    invitation and the token, which is given this once. An earlier link for the same
    email stops working: there is one way in per person."""
    email = normalize_email(email)
    if role not in ROLES:
        raise AccountError(f"role must be one of: {', '.join(ROLES)}")
    if get_user_by_email(s, email) is not None:
        raise AccountError(f"{email} already has an account")
    now = utcnow()
    for earlier in _pending(s, organization_id, purpose="join", email=email):
        earlier.revoked_at = now
    if len(_pending(s, organization_id, purpose="join")) >= MAX_PENDING:
        raise AccountError(f"{MAX_PENDING} invitations are waiting to be accepted; withdraw some first")
    return _issue(s, organization_id=organization_id, purpose="join", email=email, name=(name or "").strip()[:200], role=role, created_by=by[:254],
                  expires_at=now + timedelta(days=JOIN_DAYS))


def reset_link(s: Session, user: User, *, by: str = "") -> tuple[Invitation, str]:
    """A link by which this person chooses a new password. Using it ends their sign-ins."""
    if not user.active:
        raise AccountError("this account is deactivated; reactivate it first")
    now = utcnow()
    for earlier in _pending(s, user.organization_id, purpose="reset", user_id=user.id):
        earlier.revoked_at = now
    return _issue(s, organization_id=user.organization_id, purpose="reset", email=user.email, name=user.name, role=user.role, user_id=user.id, created_by=by[:254],
                  expires_at=now + timedelta(hours=RESET_HOURS))


def list_invitations(s: Session, organization_id: int, limit: int = 100) -> list[Invitation]:
    """Invitations to join, newest first. Reset links are not listed: they are about one account, on its row."""
    return list(s.execute(select(Invitation).where(Invitation.organization_id == organization_id, Invitation.purpose == "join").order_by(Invitation.id.desc()).limit(limit)).scalars())


def withdraw(s: Session, organization_id: int, invitation_id: int) -> Invitation | None:
    invitation = s.get(Invitation, invitation_id)
    if invitation is None or invitation.organization_id != organization_id:
        return None
    if state(invitation) != "pending":
        raise AccountError(f"this invitation is already {state(invitation)}")
    invitation.revoked_at = utcnow()
    return invitation


def find(s: Session, token: str) -> Invitation | None:
    """The invitation this token opens, if it still opens anything."""
    if not (token or "").startswith(TOKEN_PREFIX):
        return None
    invitation = s.execute(select(Invitation).where(Invitation.token_hash == _hash(token))).scalar_one_or_none()
    return invitation if invitation is not None and state(invitation) == "pending" else None


def preview(s: Session, token: str) -> dict[str, Any] | None:
    """What the person holding the link is shown before choosing a password."""
    invitation = find(s, token)
    if invitation is None:
        return None
    organization = s.get(Organization, invitation.organization_id)
    return {"purpose": invitation.purpose, "email": invitation.email, "name": invitation.name, "role": invitation.role,
            "organization": organization.name if organization else "", "expires_at": invitation.expires_at.isoformat()}


def accept(s: Session, token: str, password: str, *, name: str | None = None) -> LoginResult:
    """Use the link: create the account (join) or set the new password (reset), and sign
    the person in. The link stops working."""
    from ..observability import audit_entry

    invitation = find(s, token)
    if invitation is None:
        return LoginResult(ok=False, status=404, error=GONE)
    try:
        if invitation.purpose == "join":
            if get_user_by_email(s, invitation.email) is not None:
                return LoginResult(ok=False, status=409, error=f"{invitation.email} already has an account. Sign in instead.")
            user = create_user(s, invitation.email, password, name=name if name is not None and name.strip() else invitation.name, role=invitation.role,
                               organization_id=invitation.organization_id)
            invitation.user_id = user.id
            action = "invitation_accepted"
        else:
            user = s.get(User, invitation.user_id) if invitation.user_id else None
            if user is None or not user.active:
                return LoginResult(ok=False, status=404, error=GONE)
            check_password_rules(password, user.email)
            set_password(s, user, password)  # ends every sign-in of theirs: whoever had the old password is out
            action = "password_reset_by_link"
    except AccountError as e:
        return LoginResult(ok=False, status=400, error=str(e))
    invitation.used_at = utcnow()
    audit_entry(s, organization_id=user.organization_id, actor=user.email, role=user.role, action=action, method="POST", route="/api/auth/invitations/{token}", status=200)
    return open_session(s, user)
