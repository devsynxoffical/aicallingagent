import pytest

from callagent.languages import LanguageTracker, base_language, guess_latin_language, script_language


def test_base_language_and_script_detection():
    assert base_language("ar-SA") == "ar" and base_language("fr_FR") == "fr" and base_language(None) == ""
    assert script_language("ممكن تشرح لي بالعربي؟") == "ar"
    assert script_language("Can you explain your pricing?") is None
    # A brand name in Latin letters inside an Arabic sentence is still Arabic.
    assert script_language("أريد معلومات عن BrightBooks من فضلك") == "ar"
    # One stray Arabic character is not a language switch.
    assert script_language("ok ع") is None


def test_latin_guess_needs_clear_evidence():
    assert guess_latin_language("Bonjour, est-ce que vous pouvez m'expliquer vos tarifs ?") == "fr"
    assert guess_latin_language("Can you tell me what the price is for this?") == "en"
    assert guess_latin_language("oui") is None  # too short to call
    assert guess_latin_language("BrightBooks Xero QuickBooks") is None  # names only


def test_tracker_follows_the_prospect_english_to_arabic_and_back():
    t = LanguageTracker(allowed=("en", "ar", "fr"))
    assert t.current == "en"
    assert t.observe("Can you explain your pricing?", "en") is None
    switch = t.observe("ممكن تشرح لي بالعربي؟", "ar")
    assert switch is not None and (switch.from_language, switch.to_language, switch.turn) == ("en", "ar", 2)
    assert t.current == "ar"
    assert t.observe("طيب، كم السعر؟", None) is None  # still Arabic, recognised from script alone
    assert t.observe("Okay, and how much is it per month?", "en").to_language == "en"
    report = t.report()
    assert report["final"] == "en" and report["turns_by_language"] == {"en": 2, "ar": 2}
    assert [s["to"] for s in report["switches"]] == ["ar", "en"]


def test_tracker_follows_french_from_the_recognizer_tag_or_from_the_words():
    t = LanguageTracker(allowed=("en", "fr"))
    assert t.observe("Oui", "fr").to_language == "fr"  # short, but the recognizer is sure
    t2 = LanguageTracker(allowed=("en", "fr"))
    assert t2.observe("Bonjour, je voudrais savoir combien ça coûte s'il vous plaît", None).to_language == "fr"


def test_tracker_never_leaves_the_languages_the_campaign_approved():
    t = LanguageTracker(allowed=("en-US",))
    assert t.allowed == ("en",)
    assert t.observe("ممكن تشرح لي بالعربي؟", "ar") is None  # Arabic is not approved for this campaign
    assert t.observe("Hola, ¿cómo estás?", "es") is None
    assert t.current == "en" and t.switches == []


def test_tracker_ignores_wrong_tags_and_empty_text():
    t = LanguageTracker(allowed=("en", "ar"))
    # Recognizer said Arabic but the words are Latin (a name, noise): not a switch.
    assert t.observe("Mohammed", "ar") is None and t.current == "en"
    assert t.observe("   ", "ar") is None and t.turns == 1


def test_tracker_opens_in_the_first_allowed_language_and_needs_one():
    assert LanguageTracker(allowed=("fr", "en")).current == "fr"
    assert LanguageTracker(allowed=("fr", "en"), current="de").current == "fr"
    with pytest.raises(ValueError):
        LanguageTracker(allowed=())
