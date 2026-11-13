import pytest
from fixtures import make_docx, make_pdf, make_pptx

from callagent.training.ingest import (
    MAX_UPLOAD_BYTES,
    IngestError,
    Section,
    chunk_sections,
    extract_sections,
    parse_html,
    validate_upload,
)


def test_pdf_text_is_read_page_by_page():
    data = make_pdf(["Medical billing and RCM for clinics.", "Payment posting within 24 hours."])
    assert validate_upload("services.pdf", data) == "pdf"
    sections = extract_sections("pdf", data)
    assert [(s.label, s.text) for s in sections] == [
        ("page 1", "Medical billing and RCM for clinics."),
        ("page 2", "Payment posting within 24 hours."),
    ]


def test_scanned_pdf_is_refused_with_a_reason_the_uploader_can_act_on():
    with pytest.raises(IngestError, match="scanned PDF"):
        extract_sections("pdf", make_pdf(["", ""]))


def test_word_document_is_split_at_headings_and_keeps_tables():
    sections = extract_sections("docx", make_docx())
    assert [s.label for s in sections] == ["Pricing", "Support"]
    assert "forty-nine dollars" in sections[0].text and "Growth | $99" in sections[0].text
    assert sections[1].text == "Support is available on weekdays from nine to six."


def test_powerpoint_gives_slide_text_and_speaker_notes():
    sections = extract_sections("pptx", make_pptx())
    assert sections[0].label == "slide 1"
    assert "Payment posting" in sections[0].text and "within 24 hours" in sections[0].text
    assert "Speaker notes: Mention ERA reconciliation." in sections[0].text


def test_html_drops_scripts_navigation_and_footers():
    html = (
        "<html><head><title>Acme Billing</title><script>track()</script><style>p{}</style></head><body>"
        "<nav>Home | Login</nav><h1>Services</h1><p>We handle claims &amp; denials.</p>"
        "<h2>Contact</h2><p>Call us. <a href='/pricing'>Pricing</a></p><footer>(c) Acme</footer></body></html>"
    )
    title, sections, links = parse_html(html)
    assert title == "Acme Billing" and links == ["/pricing"]
    assert [(s.label, s.text) for s in sections] == [("Services", "We handle claims & denials."), ("Contact", "Call us. Pricing")]
    assert extract_sections("html", html.encode())[0].text == "We handle claims & denials."


def test_csv_rows_become_labelled_facts():
    sections = extract_sections("csv", "Service;Price\nBilling;$49\nPosting;$20\n".encode())
    assert sections[0].text == "Service: Billing; Price: $49\nService: Posting; Price: $20"


def test_text_in_arabic_and_legacy_encodings_is_decoded():
    assert extract_sections("txt", "نقدم خدمات الفوترة الطبية".encode("utf-8"))[0].text == "نقدم خدمات الفوترة الطبية"
    assert extract_sections("txt", "Tarifs à partir de 49 €".encode("cp1252"))[0].text.startswith("Tarifs à partir")


@pytest.mark.parametrize(
    "filename, data, reason",
    [
        ("malware.exe", b"MZ\x90", "not supported"),
        ("notes.txt", b"", "empty"),
        ("fake.pdf", b"<html>not a pdf</html>", "not a PDF"),
        ("fake.docx", b"plain text", "not an Office document"),
        ("broken.docx", b"PK\x03\x04garbage", "damaged"),
        ("binary.txt", b"abc\x00\x00def", "binary"),
        ("huge.txt", b"a" * (MAX_UPLOAD_BYTES + 1), "larger than"),
    ],
)
def test_bad_uploads_are_refused_before_they_are_stored(filename, data, reason):
    with pytest.raises(IngestError, match=reason):
        validate_upload(filename, data)


def test_a_word_file_renamed_to_pptx_is_caught():
    with pytest.raises(IngestError, match="different kind of Office document"):
        validate_upload("deck.pptx", make_docx())


def test_passages_stay_inside_their_section_and_overlap_within_it():
    long_page = Section("page 1", "\n\n".join(f"Paragraph {i}. " + "billing " * 35 for i in range(8)))
    passages = chunk_sections([long_page, Section("page 2", "Refunds are issued within ten days.")], "handbook.pdf")
    assert len(passages) >= 3 and all(len(p.text) <= 1100 for p in passages)
    assert passages[-1].source_label == "handbook.pdf, page 2" and passages[-1].text == "Refunds are issued within ten days."
    assert all(p.source_label == "handbook.pdf, page 1" for p in passages[:-1])
    # Nothing is lost: every paragraph appears in some passage.
    joined = " ".join(p.text for p in passages)
    assert all(f"Paragraph {i}." in joined for i in range(8))
    # The start of a passage repeats the end of the previous one.
    assert passages[1].text.split("\n\n")[0] in passages[0].text


def test_one_enormous_paragraph_is_cut_at_sentence_ends():
    text = " ".join(f"Sentence number {i} explains one more detail of the service." for i in range(60))
    passages = chunk_sections([Section("", text)], "faq.txt")
    assert len(passages) > 2 and all(len(p.text) <= 1100 for p in passages)
    assert all(p.source_label == "faq.txt" for p in passages)
    assert passages[0].text.endswith(".")
