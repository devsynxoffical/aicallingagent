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


def test_agent_says_it_is_an_ai_up_front_and_never_claims_to_be_human(sample_playbook):
    prompt = build_system_prompt(sample_playbook, {"first_name": "Maria"})
    assert "In your very first turn, say that you are an AI assistant for BrightBooks" in prompt
    assert "Never claim to be a human" in prompt
    assert "Never invent a guarantee, discount, testimonial" in prompt
    sample_playbook.disclose_ai_upfront = False
    assert "In your very first turn" not in build_system_prompt(sample_playbook, {"first_name": "Maria"})


def test_recorded_calls_tell_the_prospect(sample_playbook):
    assert "This call is being recorded" not in build_system_prompt(sample_playbook, {})
    recorded = build_system_prompt(sample_playbook, {}, call_recorded=True)
    assert "This call is being recorded. Say so in your first turn" in recorded
    assert "If they object to being recorded" in recorded


def test_single_language_campaign_stays_in_its_language(sample_playbook):
    prompt = build_system_prompt(sample_playbook, {})
    assert "Speak English (en-US) for the whole call." in prompt
    assert "You speak English, Arabic" not in prompt


def test_multilingual_campaign_follows_the_prospect(sample_playbook):
    sample_playbook.additional_languages = ["ar-SA", "fr"]
    prompt = build_system_prompt(sample_playbook, {})
    assert "You speak English, Arabic, French on this call. Open in English." in prompt
    assert "Always answer in the language of the prospect's most recent message" in prompt
    assert "One language per reply" in prompt and "Do not translate your" in prompt
    assert "Gulf, Saudi, Emirati, Egyptian" in prompt and "Use vous" in prompt
    # The voicemail prompt carries the language rules too.
    assert "You speak English, Arabic, French" in build_system_prompt(sample_playbook, {}, mode="voicemail")


def test_native_phrasing_is_included_per_language(sample_playbook):
    from callagent.playbook.schema import LocalizedPhrasing, Objection

    sample_playbook.additional_languages = ["fr"]
    sample_playbook.localized = [
        LocalizedPhrasing(
            language="fr-FR",
            opening_line="Bonjour, ici Jordan, l'assistant IA de BrightBooks.",
            permission_line="Vous avez une minute ?",
            pitch="On fait gagner une journée par semaine à vos comptables.",
            objections=[Objection(objection="Envoyez-moi un email", response="Avec plaisir, je vous envoie un lien.")],
            booking_phrases=["Mardi à dix heures, ça vous irait ?"],
            closing_script="Parfait, c'est noté.",
            voicemail_script="Bonjour, Jordan de BrightBooks, rappelez-moi au {callback_number}.",
            style_notes="Vouvoiement, ton chaleureux.",
        )
    ]
    assert sample_playbook.localized_for("fr").opening_line.startswith("Bonjour")
    assert sample_playbook.localized_for("ar") is None
    prompt = build_system_prompt(sample_playbook, {})
    assert "## When speaking French" in prompt and "l'assistant IA de BrightBooks" in prompt
    assert "Mardi à dix heures" in prompt and "Avec plaisir, je vous envoie un lien." in prompt


def test_inbound_prompt_greets_instead_of_asking_permission(sample_playbook):
    prompt = build_system_prompt(sample_playbook, {}, mode="inbound")
    assert "# This is an inbound call" in prompt and "you did not call them" in prompt
    assert "Opening (after they say hello)" not in prompt  # the outbound opener is not used
    assert "How many clients?" in prompt  # qualification still applies


def test_playbooks_saved_before_languages_existed_still_load(sample_playbook):
    from callagent.playbook.schema import Playbook

    old = sample_playbook.model_dump()
    for key in ("additional_languages", "localized", "disclose_ai_upfront"):
        old.pop(key)
    loaded = Playbook.model_validate(old)
    assert loaded.languages == ["en"] and loaded.disclose_ai_upfront is True and loaded.localized == []
