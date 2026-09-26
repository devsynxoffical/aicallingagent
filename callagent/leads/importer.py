"""Import a lead sheet (CSV, XLSX or a Google Sheets URL) into a campaign."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import phonenumbers
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Campaign, Lead, is_dnc

PHONE_COLUMNS = ("phone", "phone_number", "mobile", "cell", "telephone", "tel", "number", "phone number", "mobile number")
FIRST_COLUMNS = ("first_name", "first name", "firstname", "given_name", "first")
LAST_COLUMNS = ("last_name", "last name", "lastname", "surname", "family_name", "last")
NAME_COLUMNS = ("name", "full_name", "full name", "contact", "contact name", "owner")
COMPANY_COLUMNS = ("company", "business", "company name", "business name", "organization", "organisation", "account")
EMAIL_COLUMNS = ("email", "e-mail", "email address")
TZ_COLUMNS = ("timezone", "time zone", "tz")
NOTES_COLUMNS = ("notes", "note", "comments", "comment")


@dataclass
class ImportReport:
    imported: int = 0
    invalid_phone: int = 0
    duplicates: int = 0
    dnc: int = 0
    column_map: dict[str, str] = field(default_factory=dict)
    samples_invalid: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "invalid_phone": self.invalid_phone,
            "duplicates": self.duplicates,
            "dnc": self.dnc,
            "column_map": self.column_map,
            "samples_invalid": self.samples_invalid[:5],
        }


def _find_col(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {c.lower().strip(): c for c in columns}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    for cand in candidates:
        for low, orig in lowered.items():
            if cand in low:
                return orig
    return None


def normalize_phone(raw: Any, default_region: str = "US") -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return None
    # Excel often turns numbers into floats like 15551234567.0
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    try:
        parsed = phonenumbers.parse(text, None if text.startswith("+") else default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _gsheet_to_csv_url(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if not m:
        raise ValueError("Not a Google Sheets URL")
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


def load_sheet(source: str | Path) -> pd.DataFrame:
    """Load CSV / XLSX from disk, or a Google Sheets link shared as 'anyone with the link'."""
    src = str(source)
    if src.startswith("http://") or src.startswith("https://"):
        url = _gsheet_to_csv_url(src) if "docs.google.com/spreadsheets" in src else src
        resp = httpx.get(url, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        return pd.read_csv(io.StringIO(resp.text), dtype=str)
    path = Path(src)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str)


def map_columns(df: pd.DataFrame) -> dict[str, str]:
    cols = [str(c) for c in df.columns]
    mapping: dict[str, str] = {}
    for key, cands in (
        ("phone", PHONE_COLUMNS),
        ("first_name", FIRST_COLUMNS),
        ("last_name", LAST_COLUMNS),
        ("name", NAME_COLUMNS),
        ("company", COMPANY_COLUMNS),
        ("email", EMAIL_COLUMNS),
        ("timezone", TZ_COLUMNS),
        ("notes", NOTES_COLUMNS),
    ):
        if key == "name" and "first_name" in mapping:
            continue  # a full-name column only matters when there is no first-name column
        col = _find_col(cols, cands)
        if col and col not in mapping.values():
            mapping[key] = col
    if "phone" not in mapping:
        raise ValueError(f"Could not find a phone column. Columns seen: {cols}")
    return mapping


def rows_to_leads(df: pd.DataFrame, mapping: dict[str, str], default_region: str) -> tuple[list[dict[str, Any]], ImportReport]:
    report = ImportReport(column_map=mapping)
    mapped_cols = set(mapping.values())
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        raw_phone = row.get(mapping["phone"])
        e164 = normalize_phone(raw_phone, default_region)
        if not e164:
            report.invalid_phone += 1
            if raw_phone is not None and str(raw_phone) != "nan":
                report.samples_invalid.append(str(raw_phone))
            continue
        if e164 in seen:
            report.duplicates += 1
            continue
        seen.add(e164)

        def val(key: str) -> str:
            col = mapping.get(key)
            if not col:
                return ""
            v = row.get(col)
            return "" if v is None or str(v) == "nan" else str(v).strip()

        first, last = val("first_name"), val("last_name")
        if not first and val("name"):
            bits = val("name").split(None, 1)
            first = bits[0]
            last = bits[1] if len(bits) > 1 else last
        extra = {
            str(c): ("" if row[c] is None or str(row[c]) == "nan" else str(row[c]).strip())
            for c in df.columns
            if str(c) not in mapped_cols
        }
        out.append(
            {
                "phone_e164": e164,
                "raw_phone": str(raw_phone),
                "first_name": first,
                "last_name": last,
                "company": val("company"),
                "email": val("email"),
                "timezone": val("timezone") or None,
                "notes": val("notes"),
                "extra": {k: v for k, v in extra.items() if v},
            }
        )
    return out, report


def import_leads(session: Session, campaign: Campaign, source: str | Path, default_region: str = "US") -> ImportReport:
    df = load_sheet(source)
    mapping = map_columns(df)
    rows, report = rows_to_leads(df, mapping, default_region)
    existing = set(
        session.execute(select(Lead.phone_e164).where(Lead.campaign_id == campaign.id)).scalars().all()
    )
    for r in rows:
        if r["phone_e164"] in existing:
            report.duplicates += 1
            continue
        if is_dnc(session, r["phone_e164"]):
            report.dnc += 1
            continue
        session.add(Lead(campaign_id=campaign.id, **r))
        existing.add(r["phone_e164"])
        report.imported += 1
    session.flush()
    return report
