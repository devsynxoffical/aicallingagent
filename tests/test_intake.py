"""Website form submissions: handed over by the website, worked in the console."""

import pytest
from fastapi.testclient import TestClient

from callagent import db
from callagent.config import get_settings, resolve_api_token

INTAKE = {"Authorization": "Bearer website-intake-token"}
PASSWORD = "correct horse battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INTAKE_TOKEN", "website-intake-token")
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c
    get_settings.cache_clear()


def test_the_website_hands_over_a_request_and_it_appears_in_the_list(client):
    sent = client.post("/api/intake/requests", headers=INTAKE, json={"kind": "demo", "email": " Ada@Example.com ", "name": "Ada", "company": "Northwind", "team_size": "6-20", "message": "Show me the dialer."})
    assert sent.status_code == 201
    client.post("/api/intake/requests", headers=INTAKE, json={"kind": "callback", "phone": "+1 415 555 0100"})

    listed = client.get("/api/requests").json()
    assert (listed["total"], listed["new"]) == (2, 2)
    callback, demo = listed["requests"]  # newest first
    assert (demo["kind"], demo["email"], demo["company"], demo["status"]) == ("demo", "ada@example.com", "Northwind", "new")
    assert (callback["kind"], callback["phone"], callback["email"]) == ("callback", "+1 415 555 0100", None)
    assert client.get("/api/requests", params={"kind": "callback"}).json()["total"] == 1


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "demo", "name": "No way to answer"},  # no email
        {"kind": "callback", "email": "ada@example.com"},  # no number to call
        {"kind": "callback", "phone": "call me <script>"},
        {"kind": "survey", "email": "ada@example.com"},
        {"kind": "contact", "email": "ada@example.com", "message": "x" * 2001},
    ],
)
def test_a_request_nobody_could_answer_or_that_is_malformed_is_refused(client, body):
    assert client.post("/api/intake/requests", headers=INTAKE, json=body).status_code == 422
    assert client.get("/api/requests").json()["total"] == 0


def test_the_intake_token_can_only_hand_over_requests(client):
    body = {"kind": "newsletter", "email": "ada@example.com"}
    assert client.post("/api/intake/requests", json=body, headers={"Authorization": ""}).status_code == 401
    assert client.post("/api/intake/requests", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Not even the engine's own API token is accepted here, and the intake token opens nothing else.
    assert client.post("/api/intake/requests", json=body).status_code == 401
    for path in ("/api/requests", "/api/campaigns", "/api/users", "/api/auth/me"):
        assert client.get(path, headers=INTAKE).status_code == 401


def test_intake_is_off_until_a_token_is_configured(client, monkeypatch):
    monkeypatch.delenv("INTAKE_TOKEN")
    get_settings.cache_clear()
    for headers in (INTAKE, {"Authorization": "Bearer "}, {"Authorization": ""}):
        assert client.post("/api/intake/requests", json={"kind": "newsletter", "email": "a@example.com"}, headers=headers).status_code == 401


def test_handling_a_request_is_recorded_under_the_person_and_can_be_undone(client):
    client.post("/api/intake/requests", headers=INTAKE, json={"kind": "contact", "email": "ada@example.com", "name": "Ada", "message": "Hello"})
    request_id = client.get("/api/requests").json()["requests"][0]["id"]
    for role in ("viewer", "operator"):
        assert client.post("/api/users", json={"email": f"{role}@example.com", "role": role, "password": PASSWORD}).status_code == 201
    sign_in = lambda email: {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": email, "password": PASSWORD}, headers={"Authorization": ""}).json()["token"]}  # noqa: E731

    viewer = sign_in("viewer@example.com")
    assert client.get("/api/requests", headers=viewer).status_code == 200  # reads
    assert client.post(f"/api/requests/{request_id}/handled", headers=viewer).status_code == 403

    done = client.post(f"/api/requests/{request_id}/handled", headers=sign_in("operator@example.com")).json()
    assert (done["status"], done["handled_by"]) == ("handled", "operator@example.com") and done["handled_at"]
    assert client.get("/api/requests", params={"status": "new"}).json() == {"total": 0, "new": 0, "requests": []}

    back = client.post(f"/api/requests/{request_id}/handled", json={"handled": False}).json()
    assert (back["status"], back["handled_by"], back["handled_at"]) == ("new", None, None)
    assert client.post("/api/requests/999/handled").status_code == 404
