"""Joining an organization and getting back into an account by a one-time link: who
can make one, what it lets its holder do, and when it stops working."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from callagent import accounts, db
from callagent.accounts import invitations
from callagent.config import get_settings, resolve_api_token
from callagent.db import AuditEntry, Invitation, User, session_scope, utcnow
from conftest import POSTGRES_URL, postgres_app_url

PASSWORD = "correct horse battery"
NOBODY = {"Authorization": ""}  # someone who is not signed in: the person holding a link


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def client(request, tmp_path, monkeypatch):
    url = postgres_app_url() if request.param == "postgres" else f"sqlite:///{tmp_path / 'invite.db'}"
    for name, value in {"TWILIO_VALIDATE_SIGNATURE": "false", "PUBLIC_BASE_URL": "https://engine.example.com", "DATABASE_URL": url, "DATA_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    db.reset_engine_for_tests(url)
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


def sign_in(client, email, password=PASSWORD):
    answer = client.post("/api/auth/login", json={"email": email, "password": password}, headers=NOBODY)
    assert answer.status_code == 200, answer.text
    return {"Authorization": f"Bearer {answer.json()['token']}"}


@pytest.fixture
def bolt(client):
    created = client.post("/api/organizations", json={"name": "Bolt", "admin_email": "boss@bolt.example", "admin_password": PASSWORD})
    assert created.status_code == 201, created.text
    return {"id": created.json()["id"], "admin": sign_in(client, "boss@bolt.example")}


def test_a_person_joins_by_a_link_and_chooses_a_password_nobody_else_knows(client, bolt):
    made = client.post("/api/invitations", headers=bolt["admin"], json={"email": " Mina@Bolt.example ", "name": "Mina", "role": "operator"})
    assert made.status_code == 201, made.text
    token = made.json()["token"]
    assert token.startswith("dti_") and made.json()["state"] == "pending" and made.json()["email"] == "mina@bolt.example"
    listed = client.get("/api/invitations", headers=bolt["admin"])
    assert token not in listed.text and [i["email"] for i in listed.json()] == ["mina@bolt.example"]
    with session_scope() as s:  # the table cannot be used to get in: it holds a hash
        assert token not in {i.token_hash for i in s.execute(select(Invitation)).scalars()}

    # Whoever holds the link sees what it is for, without being signed in.
    shown = client.get(f"/api/auth/invitations/{token}", headers=NOBODY)
    assert shown.status_code == 200 and shown.json()["organization"] == "Bolt" and (shown.json()["purpose"], shown.json()["role"]) == ("join", "operator")

    # A password that breaks the rules is refused, and the link still works afterwards.
    assert client.post(f"/api/auth/invitations/{token}", headers=NOBODY, json={"password": "short"}).status_code == 400
    accepted = client.post(f"/api/auth/invitations/{token}", headers=NOBODY, json={"password": "mina chose this herself", "name": "Mina Okafor"})
    assert accepted.status_code == 200, accepted.text
    session = {"Authorization": f"Bearer {accepted.json()['token']}"}
    me = client.get("/api/auth/me", headers=session).json()
    assert (me["email"], me["role"], me["organization"]["name"], me["name"]) == ("mina@bolt.example", "operator", "Bolt", "Mina Okafor")
    sign_in(client, "mina@bolt.example", "mina chose this herself")

    # Used once, it is gone: for looking and for using.
    assert client.get(f"/api/auth/invitations/{token}", headers=NOBODY).status_code == 404
    assert client.post(f"/api/auth/invitations/{token}", headers=NOBODY, json={"password": "another long password"}).status_code == 404
    assert client.get("/api/invitations", headers=bolt["admin"]).json()[0]["state"] == "accepted"
    with session_scope() as s:
        actions = [(e.actor, e.action) for e in s.execute(select(AuditEntry).where(AuditEntry.organization_id == bolt["id"])).scalars()]
    assert ("mina@bolt.example", "invitation_accepted") in actions


def test_a_link_stops_working_when_it_is_withdrawn_replaced_or_too_old(client, bolt):
    def invite(email="otto@bolt.example"):
        return client.post("/api/invitations", headers=bolt["admin"], json={"email": email}).json()

    first = invite()
    second = invite()  # invited again: there is one way in per person
    assert client.get(f"/api/auth/invitations/{first['token']}", headers=NOBODY).status_code == 404
    assert client.get(f"/api/auth/invitations/{second['token']}", headers=NOBODY).status_code == 200

    assert client.delete(f"/api/invitations/{second['id']}", headers=bolt["admin"]).json()["state"] == "withdrawn"
    assert client.post(f"/api/auth/invitations/{second['token']}", headers=NOBODY, json={"password": PASSWORD}).status_code == 404
    assert client.delete(f"/api/invitations/{second['id']}", headers=bolt["admin"]).status_code == 400  # already withdrawn

    third = invite()
    with session_scope() as s:
        s.get(Invitation, third["id"]).expires_at = utcnow() - timedelta(minutes=1)
    assert client.post(f"/api/auth/invitations/{third['token']}", headers=NOBODY, json={"password": PASSWORD}).status_code == 404
    assert client.get("/api/invitations", headers=bolt["admin"]).json()[0]["state"] == "expired"
    for nonsense in ("dti_", "dti_" + "a" * 43, "something-else"):
        assert client.get(f"/api/auth/invitations/{nonsense}", headers=NOBODY).status_code == 404
    with session_scope() as s:
        assert accounts.get_user_by_email(s, "otto@bolt.example") is None


def test_invitations_are_made_by_admins_for_people_without_an_account(client, bolt):
    assert client.post("/api/invitations", headers=bolt["admin"], json={"email": "boss@bolt.example"}).status_code == 409  # already has an account
    assert client.post("/api/invitations", headers=bolt["admin"], json={"email": "x@bolt.example", "role": "owner"}).status_code == 400
    assert client.post("/api/invitations", headers=bolt["admin"], json={"email": "not an email"}).status_code == 400

    client.post("/api/users", headers=bolt["admin"], json={"email": "ops@bolt.example", "role": "operator", "password": PASSWORD})
    key = client.post("/api/keys", headers=bolt["admin"], json={"name": "CRM", "role": "admin"}).json()["key"]
    for who in (sign_in(client, "ops@bolt.example"), {"Authorization": f"Bearer {key}"}):  # an operator; a key, whatever its role
        assert client.post("/api/invitations", headers=who, json={"email": "friend@elsewhere.example", "role": "admin"}).status_code == 403
        assert client.get("/api/invitations", headers=who).status_code == 403

    # An email signs in to one account. Someone who got an account elsewhere in the meantime is told to sign in.
    token = client.post("/api/invitations", headers=bolt["admin"], json={"email": "late@bolt.example"}).json()["token"]
    client.post("/api/users", headers=bolt["admin"], json={"email": "late@bolt.example", "password": PASSWORD})
    refused = client.post(f"/api/auth/invitations/{token}", headers=NOBODY, json={"password": PASSWORD})
    assert refused.status_code == 409 and "Sign in instead" in refused.json()["detail"]


def test_a_reset_link_sets_a_new_password_and_ends_every_sign_in(client, bolt):
    user = client.post("/api/users", headers=bolt["admin"], json={"email": "mina@bolt.example", "role": "operator", "password": PASSWORD}).json()
    old_session = sign_in(client, "mina@bolt.example")
    for _ in range(accounts.MAX_FAILED_LOGINS):  # and she has locked herself out
        client.post("/api/auth/login", json={"email": "mina@bolt.example", "password": "wrong"}, headers=NOBODY)

    link = client.post(f"/api/users/{user['id']}/reset-link", headers=bolt["admin"])
    assert link.status_code == 201 and link.json()["purpose"] == "reset"
    token = link.json()["token"]
    assert client.get("/api/invitations", headers=bolt["admin"]).json() == []  # not an invitation to join: not in that list
    assert client.get(f"/api/auth/invitations/{token}", headers=NOBODY).json()["purpose"] == "reset"

    used = client.post(f"/api/auth/invitations/{token}", headers=NOBODY, json={"password": "a password only mina knows"})
    assert used.status_code == 200 and used.json()["user"]["role"] == "operator"  # the role is the account's, not the link's
    assert client.get("/api/auth/me", headers=old_session).status_code == 401  # whoever had the old password is out
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {used.json()['token']}"}).status_code == 200
    assert client.post("/api/auth/login", json={"email": "mina@bolt.example", "password": PASSWORD}, headers=NOBODY).status_code == 401
    sign_in(client, "mina@bolt.example", "a password only mina knows")  # and the lock is lifted
    assert client.post(f"/api/auth/invitations/{token}", headers=NOBODY, json={"password": "one more long password"}).status_code == 404

    # A deactivated account gets no link, and one made before it was deactivated stops working.
    stale = client.post(f"/api/users/{user['id']}/reset-link", headers=bolt["admin"]).json()["token"]
    client.patch(f"/api/users/{user['id']}", headers=bolt["admin"], json={"active": False})
    assert client.post(f"/api/users/{user['id']}/reset-link", headers=bolt["admin"]).status_code == 400
    assert client.post(f"/api/auth/invitations/{stale}", headers=NOBODY, json={"password": "one more long password"}).status_code == 404
    with session_scope() as s:
        assert s.get(User, user["id"]).active is False


def test_a_new_organization_can_be_given_to_its_admin_by_a_link(client):
    created = client.post("/api/organizations", json={"name": "Cobalt", "admin_email": "boss@cobalt.example", "admin_name": "Cy"})
    assert created.status_code == 201, created.text
    assert created.json()["admin"] is None and created.json()["invitation"]["role"] == "admin"
    token = created.json()["invitation"]["token"]
    with session_scope() as s:
        assert accounts.get_user_by_email(s, "boss@cobalt.example") is None  # nobody typed a password for them
    accepted = client.post(f"/api/auth/invitations/{token}", headers=NOBODY, json={"password": "cy chose this password"})
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {accepted.json()['token']}"}).json()
    assert (me["role"], me["organization"]["name"], me["name"]) == ("admin", "Cobalt", "Cy")
    # The name is taken either way.
    assert client.post("/api/organizations", json={"name": "Cobalt", "admin_email": "other@cobalt.example"}).status_code == 409


def test_one_organizations_invitations_are_not_another_s(client, bolt):
    other = client.post("/api/organizations", json={"name": "Cobalt", "admin_email": "boss@cobalt.example", "admin_password": PASSWORD}).json()
    cobalt = sign_in(client, "boss@cobalt.example")
    mine = client.post("/api/invitations", headers=bolt["admin"], json={"email": "mina@bolt.example"}).json()
    assert client.get("/api/invitations", headers=cobalt).json() == []
    assert client.delete(f"/api/invitations/{mine['id']}", headers=cobalt).status_code == 404
    accepted = client.post(f"/api/auth/invitations/{mine['token']}", headers=cobalt, json={"password": "mina chose this herself"})  # opened while signed in elsewhere
    with session_scope() as s:
        assert accounts.get_user_by_email(s, "mina@bolt.example").organization_id == bolt["id"] != other["id"]
    assert accepted.json()["user"]["email"] == "mina@bolt.example"
