"""Search over training documents, isolation between organizations, and the in-call lookup."""

import asyncio
from types import SimpleNamespace

import pytest

from callagent import db
from callagent.config import Settings
from callagent.playbook.prompt_builder import build_system_prompt
from callagent.training import service
from callagent.training.search import KnowledgeIndex, load_index, normalize
from callagent.voice.session import CallSession
from callagent.voice.tools import build_tools_schema, lookup_knowledge

PASSAGES = [
    ("pricing.pdf, page 1", "The Starter plan costs forty-nine dollars per client per month. Growth is ninety-nine dollars."),
    ("services.docx, Payment posting", "We post payments within 24 hours and reconcile ERAs every day."),
    ("faq.txt", "Our offices are open Monday to Friday, nine to six Eastern."),
    ("ar.txt", "نقدّم خدمات الفوترة الطبية وترحيل المدفوعات للعيادات."),
    ("fr.txt", "Nos tarifs démarrent à quarante-neuf dollars. Le déploiement prend deux semaines."),
]


def test_words_are_normalized_across_plurals_accents_and_arabic_variants():
    assert normalize("Prices, pricing and the price") == ["pric", "pric", "pric"]
    assert normalize("posted posting posts") == ["post", "post", "post"]
    assert normalize("Déploiement des tarifs") == ["deploiement", "tarif"]
    # Diacritics, tatweel and alef/teh-marbuta variants do not hide a match.
    assert normalize("الفَوْتَرة") == normalize("الفوتره")
    assert normalize("إدارة") == normalize("اداره")


def test_the_right_passage_is_found_in_each_language():
    index = KnowledgeIndex(PASSAGES)
    assert index.search("Do you provide payment posting?")[0].source == "services.docx, Payment posting"
    assert index.search("how much are the prices")[0].source == "pricing.pdf, page 1"
    assert index.search("خدمات الفوتره الطبيه")[0].source == "ar.txt"
    assert index.search("tarif deploiement")[0].source == "fr.txt"
    assert index.search("opening hours on Friday")[0].source == "faq.txt"


def test_nothing_is_returned_rather_than_something_irrelevant():
    index = KnowledgeIndex(PASSAGES)
    assert index.search("do you sell used cars") == []
    assert index.search("") == [] and index.search("the of and") == []
    assert KnowledgeIndex([]).search("pricing") == []
    # A passage that only shares a weak word with the query is dropped next to a strong match.
    hits = index.search("payment posting dollars")
    assert hits[0].source == "services.docx, Payment posting" and all(h.score >= hits[0].score * 0.4 for h in hits)


def _campaign(s, org_name: str, campaign_name: str) -> db.Campaign:
    org = db.Organization(name=org_name)
    s.add(org)
    s.flush()
    c = db.Campaign(name=campaign_name, organization_id=org.id, playbook_json={})
    s.add(c)
    s.flush()
    return c


def test_one_company_can_never_search_another_companys_documents(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'iso.db'}")
    settings = Settings(anthropic_api_key="x", data_dir=tmp_path / "data")
    with db.session_scope() as s:
        acme = _campaign(s, "acme", "acme-outreach")
        rival = _campaign(s, "rival", "rival-outreach")
        a = service.add_file(s, acme, "acme-pricing.txt", b"Acme charges forty-nine dollars per seat.", settings)
        r = service.add_file(s, rival, "rival-pricing.txt", b"Rival charges twelve dollars per seat, confidential.", settings)
        service.process_document_sync(s, a.id)
        service.process_document_sync(s, r.id)
        ids = (acme.organization_id, acme.id, rival.organization_id, rival.id)
    acme_org, acme_id, rival_org, rival_id = ids
    with db.session_scope() as s:
        acme_index = load_index(s, acme_org, acme_id)
        assert len(acme_index) == 1 and "forty-nine" in acme_index.search("price per seat")[0].text
        assert all("Rival" not in h.text for h in acme_index.search("confidential dollars seat charges"))
        # Right campaign id, wrong organization: nothing. Guessing an id is not enough.
        assert len(load_index(s, acme_org, rival_id)) == 0
        assert len(load_index(s, rival_org, acme_id)) == 0
        assert len(load_index(s, rival_org, rival_id)) == 1
    # Files are stored per organization and campaign, under names the uploader did not choose.
    stored = sorted(p.relative_to(tmp_path / "data" / "uploads").as_posix() for p in (tmp_path / "data" / "uploads").rglob("*.txt"))
    assert stored == [f"{acme_org}/{acme_id}/1.txt", f"{rival_org}/{rival_id}/2.txt"]


def test_reuploading_a_file_replaces_the_old_version_in_search_but_keeps_its_record(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'ver.db'}")
    settings = Settings(anthropic_api_key="x", data_dir=tmp_path / "data")
    with db.session_scope() as s:
        c = _campaign(s, "acme", "c")
        first = service.add_file(s, c, "pricing.txt", b"Starter is forty-nine dollars.", settings)
        service.process_document_sync(s, first.id)
        second = service.add_file(s, c, "pricing.txt", b"Starter is fifty-nine dollars since January.", settings)
        assert second.version == 2
        # Until the new version is read, the old one keeps answering.
        assert "forty-nine" in load_index(s, c.organization_id, c.id).search("starter price dollars")[0].text
        service.process_document_sync(s, second.id)
        hits = load_index(s, c.organization_id, c.id).search("starter price dollars")
        assert len(hits) == 1 and "fifty-nine" in hits[0].text
        assert [(d.version, d.status) for d in service.list_documents(s, c, include_superseded=True)] == [(1, "superseded"), (2, "ready")]
        assert [d.version for d in service.list_documents(s, c)] == [2]
        with pytest.raises(service.IngestError, match="already uploaded"):
            service.add_file(s, c, "copy.txt", b"Starter is fifty-nine dollars since January.", settings)


def test_a_document_that_cannot_be_read_says_why_and_is_not_searchable(tmp_path):
    from fixtures import make_pdf

    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'fail.db'}")
    settings = Settings(anthropic_api_key="x", data_dir=tmp_path / "data")
    with db.session_scope() as s:
        c = _campaign(s, "acme", "c")
        scan = service.add_file(s, c, "scan.pdf", make_pdf([""]), settings)
        doc = service.process_document_sync(s, scan.id)
        assert doc.status == "failed" and "scanned PDF" in doc.error and doc.chunk_count == 0
        assert len(load_index(s, c.organization_id, c.id)) == 0
        assert service.remove_document(s, c, scan.id) is True
        assert service.list_documents(s, c) == []


def _session(sample_playbook, index) -> CallSession:
    return CallSession(call_run_id=1, lead={"id": 1}, campaign_id=1, playbook=sample_playbook, settings=Settings(anthropic_api_key="x"),
                       twilio_call_sid="CA1", knowledge=index)


def _ask(session, query):
    return asyncio.run(lookup_knowledge(session, SimpleNamespace(arguments={"query": query})))


def test_agent_lookup_returns_sourced_passages_or_tells_it_not_to_guess(sample_playbook):
    session = _session(sample_playbook, KnowledgeIndex(PASSAGES, languages=("en",)))
    found = _ask(session, "payment posting turnaround")
    assert found["results"][0]["source"] == "services.docx, Payment posting" and "24 hours" in found["results"][0]["text"]
    assert "Answer only from these passages" in found["note"]
    missing = _ask(session, "cryptocurrency refunds")
    assert missing["results"] == [] and "do not guess" in missing["note"]
    assert _ask(_session(sample_playbook, None), "pricing")["results"] == []
    assert _ask(_session(sample_playbook, KnowledgeIndex([])), "pricing")["results"] == []


def test_lookup_tool_and_instructions_exist_only_when_there_are_documents(sample_playbook):
    names = lambda schema: [t.name for t in schema.standard_tools]  # noqa: E731
    assert "lookup_knowledge" not in names(build_tools_schema(set(), False))
    assert "lookup_knowledge" in names(build_tools_schema(set(), False, knowledge_available=True))
    assert "lookup_knowledge" not in build_system_prompt(sample_playbook, {}).split("# Tools")[0]
    prompt = build_system_prompt(sample_playbook, {}, knowledge_languages=("en",))
    assert "# Looking things up" in prompt and "The documents are written in English" in prompt
    assert "Never fill" in prompt and "the gap from general knowledge" in prompt
