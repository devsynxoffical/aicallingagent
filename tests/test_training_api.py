"""The training studio over HTTP: upload, status, search preview, train to a draft, approve."""

import pytest
from fastapi.testclient import TestClient
from fixtures import make_docx, make_pdf

from callagent import db
from callagent.config import get_settings, resolve_api_token
from callagent.playbook.schema import OpenQuestion, Playbook
from callagent.training import builder


@pytest.fixture
def client(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'train.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'train.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        ready = sample_playbook.model_copy(update={"open_questions": []})
        c.post("/api/campaigns", json={"name": "medbilling", "playbook": ready.model_dump(), "script_text": "We do medical billing."})
        yield c
    get_settings.cache_clear()


class FakeAnalyzer:
    """Stands in for the model: returns a playbook that reflects what it was shown."""

    def __init__(self, blocking: bool = False):
        self.blocking = blocking
        self.material = ""
        self.previous = None

    def analyze(self, material, answers=None, previous=None):
        self.material, self.previous = material, previous
        pb = previous.model_copy(deep=True)
        pb.pricing_and_terms = "Forty-nine dollars per client per month." if "forty-nine" in material else ""
        pb.persona.language = "fr-FR"  # a model must not be able to change this
        pb.additional_languages = ["es"]
        pb.open_questions = (
            [OpenQuestion(question="Two documents give different prices. Which is right?", why_it_matters="Quoting", blocking=True, suggested_default="")]
            if self.blocking
            else []
        )
        return pb


def _train_with(monkeypatch, analyzer_factory):
    """Run training jobs with a stand-in for the model."""

    async def run(run_id):
        await builder.run_training(run_id, analyzer_factory=analyzer_factory)

    monkeypatch.setattr("callagent.training.api.run_training", run)


def _upload(client, name, data, content_type="application/octet-stream"):
    return client.post("/api/campaigns/medbilling/documents", files={"file": (name, data, content_type)})


def test_training_routes_need_the_api_token(client):
    assert client.get("/api/campaigns/medbilling/documents", headers={"Authorization": ""}).status_code == 401
    assert client.post("/api/campaigns/medbilling/train", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/campaigns/unknown/documents").status_code == 404


def test_upload_is_read_in_the_background_and_becomes_searchable(client):
    r = _upload(client, "services.pdf", make_pdf(["Medical billing and RCM for clinics.", "Payment posting within 24 hours."]))
    assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["file_type"] == "pdf"
    r = _upload(client, "pricing.docx", make_docx())
    assert r.status_code == 201

    docs = client.get("/api/campaigns/medbilling/documents").json()
    assert [(d["name"], d["status"], d["language"]) for d in docs] == [("services.pdf", "ready", "en"), ("pricing.docx", "ready", "en")]
    assert docs[0]["passages"] == 2 and docs[1]["passages"] == 2 and docs[0]["error"] is None

    found = client.get("/api/campaigns/medbilling/knowledge/search", params={"q": "how fast is payment posting"}).json()
    assert found["passages_indexed"] == 4
    assert found["results"][0]["source"] == "services.pdf, page 2" and "24 hours" in found["results"][0]["text"]
    price = client.get("/api/campaigns/medbilling/knowledge/search", params={"q": "starter plan price"}).json()
    assert price["results"][0]["source"] == "pricing.docx, Pricing"
    assert client.get("/api/campaigns/medbilling/knowledge/search", params={"q": "helicopter rental"}).json()["results"] == []


def test_bad_uploads_get_a_clear_reason_and_leave_nothing_behind(client, tmp_path):
    assert "not supported" in _upload(client, "tool.exe", b"MZ").json()["detail"]
    assert "not a PDF" in _upload(client, "fake.pdf", b"hello").json()["detail"]
    assert _upload(client, "notes.txt", b"Claims are filed daily.").status_code == 201
    duplicate = _upload(client, "same-notes.txt", b"Claims are filed daily.")
    assert duplicate.status_code == 400 and "already uploaded as 'notes.txt'" in duplicate.json()["detail"]
    assert len(client.get("/api/campaigns/medbilling/documents").json()) == 1

    scan = _upload(client, "scan.pdf", make_pdf([""]))
    assert scan.status_code == 201  # accepted, then found unreadable
    failed = [d for d in client.get("/api/campaigns/medbilling/documents").json() if d["name"] == "scan.pdf"][0]
    assert failed["status"] == "failed" and "scanned PDF" in failed["error"] and failed["passages"] == 0


def test_a_path_in_the_filename_cannot_escape_the_upload_folder(client, tmp_path):
    r = _upload(client, "../../../../etc/cron.d/evil.txt", b"Refunds within ten days.")
    assert r.status_code == 201 and r.json()["name"] == "evil.txt"
    stored = [p for p in (tmp_path / "data").rglob("*.txt")]
    assert len(stored) == 1 and (tmp_path / "data" / "uploads") in stored[0].parents and stored[0].name == f"{r.json()['id']}.txt"


def test_deleting_a_document_removes_what_was_learned_from_it(client, tmp_path):
    doc = _upload(client, "notes.txt", b"Refunds are issued within ten days.").json()
    assert client.get("/api/campaigns/medbilling/knowledge/search", params={"q": "refunds"}).json()["passages_indexed"] == 1
    assert client.delete(f"/api/campaigns/medbilling/documents/{doc['id']}").status_code == 204
    assert client.delete(f"/api/campaigns/medbilling/documents/{doc['id']}").status_code == 404
    assert client.get("/api/campaigns/medbilling/knowledge/search", params={"q": "refunds"}).json() == {"passages_indexed": 0, "results": []}
    assert list((tmp_path / "data").rglob("*.txt")) == []


def test_website_sources_must_be_public_addresses(client):
    for url in ("http://127.0.0.1:80/admin", "http://169.254.169.254/latest/meta-data/", "file:///etc/passwd"):
        r = client.post("/api/campaigns/medbilling/sources", json={"url": url})
        assert r.status_code == 400, url
    assert client.post("/api/campaigns/medbilling/sources", json={"url": "https://acme.test", "max_pages": 500}).status_code == 422
    assert client.get("/api/campaigns/medbilling/documents").json() == []


def test_website_source_is_crawled_and_indexed(tmp_path):
    from callagent.config import Settings
    from callagent.training import service
    from callagent.training.search import load_index

    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'web.db'}")
    page = lambda body: (200, {"content-type": "text/html"}, f"<html><head><title>Acme</title></head><body>{body}</body></html>".encode())  # noqa: E731
    pages = {
        "https://acme.test/": page("<h1>Acme Billing</h1><p>Medical billing for clinics.</p><a href='/pricing'>Pricing</a>"),
        "https://acme.test/pricing": page("<h2>Plans</h2><p>Starter is forty-nine dollars per month.</p>"),
    }
    fetch = lambda url: pages.get(url, (404, {}, b""))  # noqa: E731
    resolve = lambda host: ["93.184.216.34"]  # noqa: E731
    with db.session_scope() as s:
        c = db.Campaign(name="web", playbook_json={})
        s.add(c)
        s.flush()
        doc = service.add_website(s, c, "https://acme.test/", 5, resolver=resolve)
        done = service.process_document_sync(s, doc.id, fetcher=fetch, resolver=resolve)
        assert (done.status, done.file_type, done.chunk_count) == ("ready", "url", 2)
        hit = load_index(s, db.campaign_organization_id(s, c), c.id).search("starter plan price")[0]
        assert "forty-nine" in hit.text and "https://acme.test/pricing" in hit.source


def test_training_produces_a_draft_and_live_calls_keep_the_current_version(client, monkeypatch):
    _upload(client, "pricing.txt", b"The Starter plan costs forty-nine dollars per client per month.")
    analyzer = FakeAnalyzer()
    _train_with(monkeypatch, lambda: analyzer)

    started = client.post("/api/campaigns/medbilling/train")
    assert started.status_code == 202 and started.json()["status"] == "running"
    run = client.get("/api/campaigns/medbilling/train").json()
    assert run["status"] == "done" and run["draft_version"] == 2 and run["error"] is None

    # The model saw the owner's script first, then the document with its source label.
    assert analyzer.material.startswith("## Sales script written by the business\nWe do medical billing.")
    assert "## Document: pricing.txt" in analyzer.material and "[pricing.txt]" in analyzer.material
    assert analyzer.previous.company_name == "BrightBooks"

    versions = client.get("/api/campaigns/medbilling/versions").json()
    assert [(v["version"], v["status"], v["source"]) for v in versions] == [(2, "draft", "training"), (1, "active", "api")]
    assert versions[0]["note"] == "Trained from pricing.txt."
    draft = client.get("/api/campaigns/medbilling/versions/2").json()["playbook"]
    assert draft["pricing_and_terms"] == "Forty-nine dollars per client per month."
    # Language settings are a person's choice; a training run does not change them.
    assert draft["persona"]["language"] == "en-US" and draft["additional_languages"] == []

    with db.session_scope() as s:
        live = Playbook.model_validate(db.get_campaign_by_name(s, "medbilling").playbook_json)
        assert live.pricing_and_terms == "Starts at $49 per client per month."  # unchanged until approval

    approved = client.post("/api/campaigns/medbilling/versions/2/approve", json={"approved_by": "sara@acme.test"})
    assert approved.status_code == 200 and approved.json()["status"] == "active" and approved.json()["approved_by"] == "sara@acme.test"
    with db.session_scope() as s:
        assert db.get_campaign_by_name(s, "medbilling").playbook_json["pricing_and_terms"] == "Forty-nine dollars per client per month."
    # Roll back to version 1.
    assert client.post("/api/campaigns/medbilling/versions/1/approve").json()["status"] == "active"
    with db.session_scope() as s:
        assert db.get_campaign_by_name(s, "medbilling").playbook_json["pricing_and_terms"] == "Starts at $49 per client per month."


def test_a_draft_with_knowledge_gaps_needs_them_answered_or_an_explicit_override(client, monkeypatch):
    analyzer = FakeAnalyzer(blocking=True)
    _train_with(monkeypatch, lambda: analyzer)
    client.post("/api/campaigns/medbilling/train")
    listed = client.get("/api/campaigns/medbilling/versions").json()[0]
    assert listed["blocking_questions"] == ["Two documents give different prices. Which is right?"]
    refused = client.post("/api/campaigns/medbilling/versions/2/approve")
    assert refused.status_code == 400 and "different prices" in refused.json()["detail"]
    rejected = client.post("/api/campaigns/medbilling/versions/2/reject", json={"reason": "Fix the price sheet first."})
    assert rejected.json()["status"] == "rejected"
    assert client.post("/api/campaigns/medbilling/versions/2/approve", json={"force": True}).status_code == 400
    assert client.get("/api/campaigns/medbilling/versions/7").status_code == 404


def test_a_failed_training_run_reports_why_and_changes_nothing(client, monkeypatch):
    def broken():
        raise RuntimeError("model unavailable")

    _train_with(monkeypatch, broken)
    client.post("/api/campaigns/medbilling/train")
    run = client.get("/api/campaigns/medbilling/train").json()
    assert run["status"] == "failed" and "model unavailable" in run["error"] and run["draft_version"] is None
    assert [v["version"] for v in client.get("/api/campaigns/medbilling/versions").json()] == [1]
    assert client.post("/api/campaigns/medbilling/train").status_code == 202  # a failed run does not block the next one


def test_long_documents_are_cut_for_the_draft_but_the_script_never_is(tmp_path, sample_playbook):
    from callagent.config import Settings
    from callagent.training import service

    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'budget.db'}")
    settings = Settings(anthropic_api_key="x", data_dir=tmp_path / "data")
    with db.session_scope() as s:
        c = db.Campaign(name="big", playbook_json={}, script_text="OUR SCRIPT " * 50)
        s.add(c)
        s.flush()
        for i in range(3):
            body = "\n\n".join(f"Document {i} paragraph {n} about claims and denials management." for n in range(400)).encode()
            service.process_document_sync(s, service.add_file(s, c, f"manual-{i}.txt", body, settings).id)
        material, used, shortened = builder.gather_material(s, c, budget=12_000)
        assert material.count("OUR SCRIPT") == 50 and len(material) < 14_000
        assert used == ["manual-0.txt", "manual-1.txt", "manual-2.txt"] and shortened == used
        assert "[... the rest of this document was not included ...]" in material
        empty = db.Campaign(name="empty", playbook_json={})
        s.add(empty)
        s.flush()
        with pytest.raises(builder.TrainingError, match="nothing to learn from"):
            builder.build_draft(s, empty, FakeAnalyzer())
