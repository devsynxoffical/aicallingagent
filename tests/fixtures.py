"""Plain constructors shared by tests and smoke scripts."""

from callagent.playbook.schema import (
    CallStage,
    Objection,
    OpenQuestion,
    Persona,
    Playbook,
    QualificationQuestion,
    RequiredCapability,
)


def make_sample_playbook() -> Playbook:
    return Playbook(
        company_name="BrightBooks",
        company_description="Bookkeeping automation for accounting firms.",
        offer_summary="AI categorization on top of Xero/QuickBooks that saves 6-10 hours per client per month.",
        target_customer="Accounting firms with 3-30 staff.",
        value_props=["Get a day back per bookkeeper per week", "Nothing changes for clients"],
        proof_points=["400+ firms", "4.8 on Capterra"],
        call_goal="book_meeting",
        call_goal_details="A 20-minute demo booked with the owner or practice manager.",
        persona=Persona(
            agent_name="Jordan",
            role_title="from the team at BrightBooks",
            tone="warm, upbeat, direct",
            speaking_style_notes="Short sentences.",
            language="en-US",
        ),
        opening_line="Hi, is this {first_name}? This is Jordan from BrightBooks.",
        permission_line="Do you have a quick minute?",
        stages=[CallStage(name="Open", goal="Get permission", talk_track="Be brief", success_signal="They say yes")],
        qualification_questions=[QualificationQuestion(question="How many clients?", purpose="Size", disqualify_if="fewer than 15")],
        objections=[Objection(objection="Send me an email", response="Happy to text you a link instead.")],
        pricing_and_terms="Starts at $49 per client per month.",
        must_say=[],
        must_not_say=["certified by Xero"],
        closing_script="Offer two slots.",
        voicemail_script="Hi, Jordan from BrightBooks, call me back at {callback_number}.",
        callback_policy="Offer a callback if busy.",
        disclose_ai_if_asked=True,
        required_capabilities=[RequiredCapability(key="book_meeting", description="Book demos", required=True)],
        keyterms=["BrightBooks", "Xero", "QuickBooks"],
        open_questions=[
            OpenQuestion(question="Which demo slots are available?", why_it_matters="Booking", blocking=True, suggested_default="")
        ],
        readiness_score=70,
        readiness_notes="Needs calendar availability.",
    )


def make_pdf(pages: list[str]) -> bytes:
    """A small real PDF with one line of selectable text per page ("" = a page with no text,
    like a scan)."""
    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode() if text else b""
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_docx() -> bytes:
    import io

    import docx

    d = docx.Document()
    d.add_heading("Pricing", 1)
    d.add_paragraph("The Starter plan costs forty-nine dollars per client per month.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Plan", "Price"
    table.cell(1, 0).text, table.cell(1, 1).text = "Growth", "$99"
    d.add_heading("Support", 1)
    d.add_paragraph("Support is available on weekdays from nine to six.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def make_pptx() -> bytes:
    import io

    import pptx

    p = pptx.Presentation()
    slide = p.slides.add_slide(p.slide_layouts[1])
    slide.shapes.title.text = "Payment posting"
    slide.placeholders[1].text = "We post payments within 24 hours."
    slide.notes_slide.notes_text_frame.text = "Mention ERA reconciliation."
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()
