"""People, passwords, sessions and roles."""

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from callagent import accounts, db
from callagent.config import get_settings, resolve_api_token
from callagent.db import User, UserSession, session_scope, utcnow

PASSWORD = "correct horse battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c
    get_settings.cache_clear()


def add_user(client, email, role, password=PASSWORD, name=""):
    response = client.post("/api/users", json={"email": email, "role": role, "password": password, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def sign_in(client, email, password=PASSWORD):
    """Headers of a signed-in person."""
    response = client.post("/api/auth/login", json={"email": email, "password": password}, headers={"Authorization": ""})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


# ----------------------------------------------------------------------------- passwords


def test_a_password_is_stored_as_a_salted_hash_and_checked_against_it():
    first, second = accounts.hash_password(PASSWORD), accounts.hash_password(PASSWORD)
    assert PASSWORD not in first and first != second  # salted: the same password never hashes the same twice
    assert accounts.verify_password(PASSWORD, first) and accounts.verify_password(PASSWORD, second)
    assert not accounts.verify_password(PASSWORD + "x", first)
    assert not accounts.verify_password(PASSWORD, "not-a-hash")
    assert not accounts.verify_password(PASSWORD, "md5$1$1$1$AAAA$AAAA")


@pytest.mark.parametrize("password", ["short", " " * 12, "x" * 201, "ada@example.com"])
def test_weak_passwords_are_refused(password):
    with pytest.raises(accounts.AccountError):
        accounts.check_password_rules(password, "ada@example.com")


# ----------------------------------------------------------------------------- signing in


def test_signing_in_opens_a_session_whose_token_is_not_stored(client):
    add_user(client, "Ada@Example.com", "operator", name="Ada")
    response = client.post("/api/auth/login", json={"email": " ada@example.COM ", "password": PASSWORD}, headers={"Authorization": ""})
    assert response.status_code == 200
    token = response.json()["token"]
    assert token.startswith("dts_") and response.json()["user"]["role"] == "operator"
    assert "password" not in str(response.json()).lower()

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me == {"kind": "user", "id": me["id"], "email": "ada@example.com", "name": "Ada", "role": "operator", "organization": {"id": me["organization"]["id"], "name": "default"}, "platform": False}
    with session_scope() as s:
        stored = s.execute(select(UserSession.token_hash)).scalar_one()
        assert stored != token and token not in stored
        assert PASSWORD not in s.execute(select(User.password_hash)).scalar_one()


def test_a_wrong_password_an_unknown_email_and_a_deactivated_account_get_the_same_answer(client):
    ada = add_user(client, "ada@example.com", "viewer")
    off = add_user(client, "off@example.com", "viewer")
    client.patch(f"/api/users/{off['id']}", json={"active": False})
    answers = [
        client.post("/api/auth/login", json={"email": email, "password": password}, headers={"Authorization": ""})
        for email, password in [("ada@example.com", "wrong password here"), ("nobody@example.com", PASSWORD), ("off@example.com", PASSWORD)]
    ]
    assert {(r.status_code, r.json()["detail"]) for r in answers} == {(401, "email or password is incorrect")}
    assert ada["active"] is True


def test_five_wrong_passwords_lock_the_account_even_for_the_right_one(client):
    add_user(client, "ada@example.com", "viewer")
    for _ in range(accounts.MAX_FAILED_LOGINS):
        assert client.post("/api/auth/login", json={"email": "ada@example.com", "password": "wrong password here"}, headers={"Authorization": ""}).status_code == 401
    locked = client.post("/api/auth/login", json={"email": "ada@example.com", "password": PASSWORD}, headers={"Authorization": ""})
    assert locked.status_code == 429 and "try again in" in locked.json()["detail"]

    with session_scope() as s:  # the lock runs out
        s.execute(update(User).values(locked_until=utcnow() - timedelta(seconds=1)))
    assert client.post("/api/auth/login", json={"email": "ada@example.com", "password": PASSWORD}, headers={"Authorization": ""}).status_code == 200


def test_a_session_ends_on_sign_out_on_expiry_and_when_the_account_is_deactivated(client):
    ada = add_user(client, "ada@example.com", "viewer")
    first, second, third = (sign_in(client, "ada@example.com") for _ in range(3))

    assert client.post("/api/auth/logout", headers=first).json() == {"ok": True}
    assert client.get("/api/campaigns", headers=first).status_code == 401
    assert client.get("/api/campaigns", headers=second).status_code == 200  # other sign-ins are untouched

    with session_scope() as s:
        newest = s.execute(select(UserSession).order_by(UserSession.id.desc()).limit(1)).scalar_one()
        newest.expires_at = utcnow() - timedelta(seconds=1)
    assert client.get("/api/campaigns", headers=third).status_code == 401

    client.patch(f"/api/users/{ada['id']}", json={"active": False})
    assert client.get("/api/campaigns", headers=second).status_code == 401
    assert client.get("/api/campaigns", headers={"Authorization": "Bearer dts_made-up"}).status_code == 401


def test_changing_a_password_needs_the_current_one_and_ends_other_sign_ins(client):
    ada = add_user(client, "ada@example.com", "operator")
    here, elsewhere = sign_in(client, "ada@example.com"), sign_in(client, "ada@example.com")
    new = "a different long password"

    assert client.post("/api/auth/password", json={"current_password": "not it at all", "new_password": new}, headers=here).status_code == 400
    assert client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "short"}, headers=here).status_code == 400
    assert client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": new}, headers=here).status_code == 200
    assert client.get("/api/auth/me", headers=here).status_code == 200
    assert client.get("/api/auth/me", headers=elsewhere).status_code == 401
    assert client.post("/api/auth/login", json={"email": "ada@example.com", "password": PASSWORD}, headers={"Authorization": ""}).status_code == 401
    sign_in(client, "ada@example.com", new)

    # An admin resetting it signs the person out everywhere.
    client.patch(f"/api/users/{ada['id']}", json={"password": "set by an admin, long"})
    assert client.get("/api/auth/me", headers=here).status_code == 401
    assert client.post("/api/auth/password", json={"current_password": "x", "new_password": new}).status_code == 400  # the API token has no password


# ----------------------------------------------------------------------------- roles


def api_routes(app):
    """Every method and route template under /api, as the app itself documents them."""
    for path, operations in app.openapi()["paths"].items():
        if path.startswith("/api"):
            for method in operations:
                yield method.upper(), path


def url_of(path):
    """A real URL for a route template: a campaign that does not exist, and 1 for every id."""
    return re.sub(r"\{(\w+)\}", lambda m: "no-such-campaign" if m.group(1) == "name" else "1", path)


def test_every_api_route_needs_a_sign_in_except_signing_in_itself(client):
    public = []
    for method, path in api_routes(client.app):
        response = client.request(method, url_of(path), headers={"Authorization": ""})
        if response.status_code != 401:
            public.append((method, path))
    # Signing in takes a password instead; the website's intake takes its own token and refuses everything else (see test_intake).
    assert public == [("POST", "/api/auth/login")]


def test_each_role_can_do_what_it_is_for_and_nothing_more(client):
    for role in accounts.ROLES:
        add_user(client, f"{role}@example.com", role)
    headers = {role: sign_in(client, f"{role}@example.com") for role in accounts.ROLES}
    routes = set(api_routes(client.app)) - {("POST", "/api/auth/login"), ("POST", "/api/intake/requests")}
    assert accounts.OPERATOR_ROUTES <= routes, "an operator route no longer exists: " + str(accounts.OPERATOR_ROUTES - routes)

    refused = {role: set() for role in accounts.ROLES}
    for method, path in sorted(routes - {("POST", "/api/auth/logout")}):  # signing out would end the sessions used here
        for role in accounts.ROLES:
            status = client.request(method, url_of(path), headers=headers[role]).status_code
            assert status != 401, (role, method, path)
            if status == 403:
                refused[role].add((method, path))

    changes = {(m, p) for m, p in routes if m != "GET" and not p.startswith(("/api/auth/", "/api/users"))}
    # Who has access, and the record of who did what: for admins, reading included.
    users = {(m, p) for m, p in routes if p.startswith(("/api/users", "/api/audit"))}
    assert ("GET", "/api/audit") in users
    # Organizations are managed by the deployment's operator, which no role inside an organization is.
    # So is correcting the usage ledger: that is for whoever bills, not for the organization billed.
    platform = {(m, p) for m, p in routes if p.startswith("/api/organizations") or (p.startswith("/api/usage/") and m != "GET")}
    assert platform == {("GET", "/api/organizations"), ("POST", "/api/organizations"), ("POST", "/api/usage/{line_id}/adjustments")}
    changes -= platform
    assert refused["admin"] == platform
    assert refused["operator"] == (changes - accounts.OPERATOR_ROUTES) | users | platform
    assert refused["viewer"] == changes | users | platform
    # What only an admin decides: what goes live, what it may cost, and who has access.
    for admin_only in [
        ("POST", "/api/campaigns/{name}/versions/{number}/approve"),
        ("POST", "/api/campaigns/{name}/versions/{number}/reject"),
        ("POST", "/api/campaigns/{name}/experiments/{experiment_id}/promote"),
        ("PUT", "/api/campaigns/{name}/rules"),
        ("POST", "/api/campaigns"),
        ("GET", "/api/users"),
    ]:
        assert admin_only in refused["operator"], admin_only


def test_a_refusal_says_which_role_is_needed(client):
    add_user(client, "viewer@example.com", "viewer")
    response = client.post("/api/campaigns/x/start", headers=sign_in(client, "viewer@example.com"))
    assert response.status_code == 403
    assert response.json()["detail"] == "your role (viewer) cannot do this; it needs the operator role"


def test_the_api_token_keeps_working_and_is_an_admin(client):
    me = client.get("/api/auth/me").json()
    assert me == {"kind": "token", "id": None, "email": "", "name": "API token", "role": "admin", "organization": {"id": me["organization"]["id"], "name": "default"}, "platform": True}
    assert client.get("/api/users").status_code == 200
    assert client.post("/api/auth/logout").json() == {"ok": True}  # nothing to end
    assert client.get("/api/campaigns").status_code == 200


# ----------------------------------------------------------------------------- managing users


def test_admins_manage_users_but_cannot_demote_or_deactivate_themselves(client):
    boss = add_user(client, "boss@example.com", "admin", name="Boss")
    admin = sign_in(client, "boss@example.com")

    created = client.post("/api/users", json={"email": "new@example.com", "role": "operator", "password": PASSWORD}, headers=admin)
    assert created.status_code == 201 and "password" not in created.text
    assert client.post("/api/users", json={"email": "NEW@example.com", "role": "viewer", "password": PASSWORD}, headers=admin).status_code == 409
    assert client.post("/api/users", json={"email": "bad", "role": "viewer", "password": PASSWORD}, headers=admin).status_code == 400
    assert client.post("/api/users", json={"email": "x@example.com", "role": "owner", "password": PASSWORD}, headers=admin).status_code == 400
    assert client.post("/api/users", json={"email": "x@example.com", "role": "viewer", "password": "short"}, headers=admin).status_code == 400

    changed = client.patch(f"/api/users/{created.json()['id']}", json={"role": "viewer", "name": "New Person"}, headers=admin).json()
    assert (changed["role"], changed["name"]) == ("viewer", "New Person")
    assert [u["email"] for u in client.get("/api/users", headers=admin).json()] == ["boss@example.com", "new@example.com"]
    assert client.patch("/api/users/999", json={"name": "x"}, headers=admin).status_code == 404

    for own in ({"role": "viewer"}, {"active": False}):
        refused = client.patch(f"/api/users/{boss['id']}", json=own, headers=admin)
        assert refused.status_code == 400 and "ask another admin" in refused.json()["detail"]
    assert client.get("/api/auth/me", headers=admin).json()["role"] == "admin"


def test_an_approval_is_recorded_under_the_person_who_gave_it(client, sample_playbook):
    from callagent.db import get_campaign_by_name
    from callagent.training import versions

    assert client.post("/api/campaigns", json={"name": "demo", "playbook": sample_playbook.model_dump(), "script_text": "pitch"}).status_code == 200
    with session_scope() as s:
        draft = versions.propose(s, get_campaign_by_name(s, "demo"), sample_playbook.model_dump(), source="training", by="training", note="test")
        number = draft.version
    add_user(client, "boss@example.com", "admin")
    approved = client.post(f"/api/campaigns/demo/versions/{number}/approve", json={"approved_by": "someone else", "force": True}, headers=sign_in(client, "boss@example.com"))
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_by"] == "boss@example.com"


# ----------------------------------------------------------------------------- schema


def test_a_database_from_before_accounts_gets_the_tables_on_upgrade(tmp_path):
    from alembic import command
    from sqlalchemy import create_engine, inspect, text

    from callagent import migrate

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    assert migrate.upgrade(engine) == "created"
    with engine.begin() as connection:  # put it back to the release before accounts
        connection.execute(text("DROP TABLE user_sessions"))
        connection.execute(text("DROP TABLE users"))
        command.stamp(migrate._config(connection), "0004")
    assert migrate.upgrade(engine) == "upgraded"
    with engine.connect() as connection:
        assert {"users", "user_sessions"} <= set(inspect(connection).get_table_names())
        assert migrate.current_revision(connection) == migrate.head_revision()
