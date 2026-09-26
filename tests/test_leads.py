from pathlib import Path

import pandas as pd

from callagent.leads.importer import load_sheet, map_columns, normalize_phone, rows_to_leads

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_normalize_phone_variants():
    assert normalize_phone("(415) 555-0134", "US") == "+14155550134"
    assert normalize_phone("+1 212 555 0198", "US") == "+12125550198"
    assert normalize_phone("4155550134.0", "US") == "+14155550134"
    assert normalize_phone("12345", "US") is None
    assert normalize_phone(None) is None
    assert normalize_phone("030 1234567", "DE") == "+49301234567"


def test_sample_sheet_mapping_and_dedupe():
    df = load_sheet(EXAMPLES / "sample_leads.csv")
    mapping = map_columns(df)
    assert mapping["phone"] == "Phone"
    assert mapping["first_name"] == "First Name"
    assert mapping["company"] == "Company"
    assert "name" not in mapping  # first/last present, so no full-name column is claimed
    rows, report = rows_to_leads(df, mapping, "US")
    assert len(rows) == 3
    assert report.invalid_phone == 1
    assert report.duplicates == 1
    maria = rows[0]
    assert maria["phone_e164"] == "+14155550134"
    assert maria["timezone"] == "America/Los_Angeles"
    assert maria["notes"] == "Referred by partner"


def test_full_name_column_is_split():
    df = pd.DataFrame({"Name": ["Ada Lovelace"], "Mobile": ["+14155550100"], "Segment": ["SMB"]})
    rows, _ = rows_to_leads(df, map_columns(df), "US")
    assert rows[0]["first_name"] == "Ada"
    assert rows[0]["last_name"] == "Lovelace"
    assert rows[0]["extra"] == {"Segment": "SMB"}
