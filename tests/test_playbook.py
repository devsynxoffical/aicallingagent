from callagent.playbook.prompt_builder import build_system_prompt
from callagent.playbook.schema import Playbook


def test_playbook_roundtrip_and_readiness(sample_playbook):
    data = sample_playbook.model_dump_json()
    pb = Playbook.model_validate_json(data)
    assert pb.company_name == "BrightBooks"
    assert not pb.is_ready
    assert len(pb.blocking_questions) == 1
    pb.open_questions = []
    assert pb.is_ready


def test_live_prompt_contains_pitch_and_lead(sample_playbook):
    lead = {"first_name": "Maria", "company": "Lopez Accounting", "timezone": "America/Los_Angeles", "extra": {"Notes": "Referred"}}
    prompt = build_system_prompt(sample_playbook, lead, callback_number="+15550100", human_transfer_available=False)
    for needle in ("Jordan", "BrightBooks", "Maria", "Lopez Accounting", "Referred", "$49", "certified by Xero",
                   "log_call_outcome", "end_call", "America/Los_Angeles", "No human transfer"):
        assert needle in prompt, needle
    assert "I am, actually" in prompt  # AI disclosure line


def test_voicemail_prompt_is_short_mode(sample_playbook):
    prompt = build_system_prompt(sample_playbook, {"first_name": "Maria"}, mode="voicemail", callback_number="+1 555 0100")
    assert "VOICEMAIL MODE" in prompt
    assert "+1 555 0100" in prompt
    assert "log_call_outcome" not in prompt
