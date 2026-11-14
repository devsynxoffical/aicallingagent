"""People, passwords, sessions and roles.

Two kinds of caller reach ``/api``:

- **A person** signs in with an email and a password and gets a session token. What
  they may do is decided by their role (see ``required_role``).
- **The engine's API token** (``API_TOKEN`` / ``DATA_DIR/api_token``) is the owner's own
  credential, used by the command line and for first setup. It has the admin role.

Passwords are stored as scrypt hashes; session tokens as SHA-256 hashes. Neither a
password nor a token can be read back from the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import User, UserSession, default_organization_id, utcnow

ROLES = ("viewer", "operator", "admin")
RANK = {role: rank for rank, role in enumerate(ROLES)}

SESSION_HOURS = 12
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

# scrypt cost: 2**15 iterations, 8 block size: about 32 MB and tens of milliseconds per check.
_N, _R, _P, _KEY_BYTES = 2**15, 8, 1, 32
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(ValueError):
    """A request about a user that cannot be carried out. The message is written for the person."""


# ----------------------------------------------------------------------------- roles

# What an operator may change. Every other change needs the admin role, so a new
# route that changes something is admin-only until it is listed here on purpose.
OPERATOR_ROUTES = frozenset(
    {
        ("POST", "/api/campaigns/{name}/start"),
        ("POST", "/api/campaigns/{name}/pause"),
        ("POST", "/api/campaigns/{name}/dial"),
        ("POST", "/api/campaigns/{name}/leads"),
        ("POST", "/api/campaigns/{name}/documents"),
        ("DELETE", "/api/campaigns/{name}/documents/{document_id}"),
        ("POST", "/api/campaigns/{name}/sources"),
        ("POST", "/api/campaigns/{name}/train"),
        ("POST", "/api/campaigns/{name}/coach"),
        ("POST", "/api/campaigns/{name}/experiments"),
        ("POST", "/api/campaigns/{name}/experiments/{experiment_id}/stop"),
        ("POST", "/api/jobs/{job_id}/retry"),
    }
)


def required_role(method: str, route: str) -> str:
    """The least role that may call ``method`` on the route template ``route``.

    - viewer: reads everything except the list of users; manages their own session and password
    - operator: also runs campaigns day to day (see ``OPERATOR_ROUTES``)
    - admin: also decides what goes live (approve, reject, roll back, promote), changes
      calling rules and budgets, creates campaigns and manages users
    """
    method = method.upper()
    if route.startswith("/api/users"):
        return "admin"
    if route.startswith("/api/auth/"):
        return "viewer"
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    if (method, route) in OPERATOR_ROUTES:
        return "operator"
    return "admin"


def allows(role: str, needed: str) -> bool:
    return RANK.get(role, -1) >= RANK[needed]


@dataclass(frozen=True)
class Principal:
    """Who is making a request."""

    kind: str  # "token" (the engine's API token) | "user"
    role: str
    user_id: int | None = None
    email: str = ""
    name: str = ""
    session_id: int | None = None

    @property
    def label(self) -> str:
        """Who to record as having done something."""
        return self.email if self.kind == "user" else "api token"


API_TOKEN_PRINCIPAL = Principal(kind="token", role="admin", name="API token")


# ----------------------------------------------------------------------------- passwords


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_BYTES, maxmem=128 * _N * _R * 2)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(key)
        found = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=128 * int(n) * int(r) * 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(found, expected)


# Checked when the email is unknown, so an unknown email takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_password_rules(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AccountError(f"the password can have at most {MAX_PASSWORD_LENGTH} characters")
    if not password.strip():
        raise AccountError("the password cannot be only spaces")
    if email and password.strip().lower() == email.lower():
        raise AccountError("the password cannot be the email address")


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise AccountError("enter a valid email address")
    return email


# ----------------------------------------------------------------------------- users


def describe(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "active": user.active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def get_user_by_email(s: Session, email: str) -> User | None:
    return s.execute(select(User).where(User.email == (email or "").strip().lower())).scalar_one_or_none()


def list_users(s: Session) -> list[User]:
    return list(s.execute(select(User).order_by(User.active.desc(), User.email)).scalars())


def create_user(s: Session, email: str, password: str, *, name: str = "", role: str = "viewer") -> User:
    email = normalize_email(email)
    if role not in ROLES:
        raise AccountError(f"role must be one of: {', '.join(ROLES)}")
    check_password_rules(password, email)
    if get_user_by_email(s, email) is not None:
        raise AccountError(f"{email} already has an account")
    user = User(organization_id=default_organization_id(s), email=email, name=name.strip()[:200], role=role, password_hash=hash_password(password))
    s.add(user)
    s.flush()
    return user


def revoke_sessions(s: Session, user_id: int, *, keep: int | None = None) -> None:
    """End a person's sign-ins, optionally keeping the one they are using."""
    statement = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if keep is not None:
        statement = statement.where(UserSession.id != keep)
    s.execute(statement.values(revoked_at=utcnow()))


def set_password(s: Session, user: User, password: str, *, keep_session: int | None = None) -> None:
    check_password_rules(password, user.email)
    user.password_hash = hash_password(password)
    user.failed_logins = 0
    user.locked_until = None
    revoke_sessions(s, user.id, keep=keep_session)


def update_user(s: Session, user: User, *, actor: Principal, name: str | None = None, role: str | None = None, active: bool | None = None, password: str | None = None) -> User:
    own = actor.kind == "user" and actor.user_id == user.id
    if role is not None and role != user.role:
        if role not in ROLES:
            raise AccountError(f"role must be one of: {', '.join(ROLES)}")
        if own:
            raise AccountError("you cannot change your own role; ask another admin")
        user.role = role
    if active is not None and active != user.active:
        if own:
            raise AccountError("you cannot deactivate your own account; ask another admin")
        user.active = active
        if not active:
            revoke_sessions(s, user.id)
    if name is not None:
        user.name = name.strip()[:200]
    if password is not None:
        set_password(s, user, password, keep_session=actor.session_id if own else None)
    s.flush()
    return user


# ----------------------------------------------------------------------------- sessions


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class LoginResult:
    ok: bool
    status: int = 200
    error: str = ""
    token: str = ""
    expires_at: str = ""
    user: dict[str, Any] | None = None


_WRONG = "email or password is incorrect"


def login(s: Session, email: str, password: str) -> LoginResult:
    """Check a sign-in and open a session.

    Returns a result instead of raising, so that a failed attempt is still counted (an
    exception would roll the count back). The same message is used for an unknown email,
    a wrong password and a deactivated account.
    """
    user = get_user_by_email(s, email)
    now = utcnow()
    if user is None:
        verify_password(password or "", _DUMMY_HASH)
        return LoginResult(ok=False, status=401, error=_WRONG)
    if user.locked_until is not None and user.locked_until > now:
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        return LoginResult(ok=False, status=429, error=f"too many failed sign-in attempts; try again in {minutes} minutes")
    if not verify_password(password or "", user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            user.failed_logins = 0
        return LoginResult(ok=False, status=401, error=_WRONG)
    if not user.active:
        return LoginResult(ok=False, status=401, error=_WRONG)
    token = "dts_" + secrets.token_urlsafe(32)
    expires = now + timedelta(hours=SESSION_HOURS)
    s.add(UserSession(user_id=user.id, token_hash=_token_hash(token), expires_at=expires))
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    s.flush()
    return LoginResult(ok=True, token=token, expires_at=expires.isoformat(), user=describe(user))


def principal_for_session(s: Session, token: str) -> Principal | None:
    """The signed-in person a session token belongs to, or None when it is unknown, expired, ended or its account is deactivated."""
    if not token.startswith("dts_"):
        return None
    row = s.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == _token_hash(token), UserSession.revoked_at.is_(None), UserSession.expires_at > utcnow(), User.active.is_(True))
    ).first()
    if row is None:
        return None
    session, user = row
    return Principal(kind="user", role=user.role if user.role in ROLES else "viewer", user_id=user.id, email=user.email, name=user.name, session_id=session.id)


def logout(s: Session, principal: Principal) -> None:
    if principal.session_id is not None:
        s.execute(update(UserSession).where(UserSession.id == principal.session_id, UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
