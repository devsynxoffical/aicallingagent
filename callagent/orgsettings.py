"""What belongs to one organization and not to the deployment: the numbers it calls
from, where a call is handed to a person, where bookings are reported, its rates.

The rule that keeps customers apart: the **default** organization (the deployment's
own, the only one before there were several) uses the deployment's environment
settings where it has none of its own. **Every other organization uses only what was
set for it.** It never borrows the deployment's phone number, transfer number or
booking webhook: an organization without a number of its own cannot place a call,
and says so. Rates are the exception: they are the operator's prices, and apply to
every organization unless one is given its own.

Who may set what: the numbers and the rates are the deployment operator's to assign
(a caller number must be one the operator holds at the phone provider; letting a
customer type any number would be caller-ID spoofing). The transfer number and the
booking webhook are the organization's own admin's.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import DEFAULT_ORGANIZATION, Organization

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


class SettingsError(ValueError):
    """The settings asked for cannot be saved. The message is for the person asking."""


def _number(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    cleaned = re.sub(r"[\s().-]", "", str(value))
    if not E164.match(cleaned):
        raise ValueError(f"'{value}' is not a phone number in international form, such as +14155550100")
    return cleaned


class OrganizationSettings(BaseModel):
    from_numbers: list[str] = Field(default_factory=list, max_length=50)
    human_transfer_number: str | None = None
    booking_webhook_url: str | None = Field(default=None, max_length=500)
    booking_webhook_secret: str | None = Field(default=None, max_length=200)
    cost_per_connected_minute_usd: float | None = Field(default=None, ge=0, le=100)
    cost_per_call_attempt_usd: float | None = Field(default=None, ge=0, le=100)

    @field_validator("from_numbers")
    @classmethod
    def _numbers(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(n for n in (_number(v) for v in values) if n))

    @field_validator("human_transfer_number")
    @classmethod
    def _transfer(cls, value: str | None) -> str | None:
        return _number(value)

    @field_validator("booking_webhook_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if not value.strip().lower().startswith("https://"):
            raise ValueError("the booking webhook must be an https:// address")
        return value.strip()


def of(org: Organization | None) -> OrganizationSettings:
    return OrganizationSettings.model_validate((org.settings_json if org is not None else None) or {})


def is_default(org: Organization | None) -> bool:
    return org is not None and org.name == DEFAULT_ORGANIZATION


def effective(s: Session, organization_id: int | None, settings: Settings) -> Settings:
    """The deployment's settings as they apply to one organization (see the rule above)."""
    org = s.get(Organization, organization_id) if organization_id is not None else None
    if org is None:
        return settings
    own, inherits = of(org), is_default(org)
    numbers = own.from_numbers or ([settings.twilio_from_number] if inherits and settings.twilio_from_number else [])
    update: dict[str, Any] = {
        "twilio_from_numbers": numbers,
        "twilio_from_number": numbers[0] if numbers else None,
        "human_transfer_number": own.human_transfer_number or (settings.human_transfer_number if inherits else None),
        "booking_webhook_url": own.booking_webhook_url or (settings.booking_webhook_url if inherits else None),
        "booking_webhook_secret": own.booking_webhook_secret if own.booking_webhook_url else (settings.booking_webhook_secret if inherits else None),
    }
    if own.cost_per_connected_minute_usd is not None:
        update["cost_per_connected_minute_usd"] = own.cost_per_connected_minute_usd
    if own.cost_per_call_attempt_usd is not None:
        update["cost_per_call_attempt_usd"] = own.cost_per_call_attempt_usd
    if all(getattr(settings, name) == value for name, value in update.items()):
        return settings
    return settings.model_copy(update=update)


def number_for(settings: Settings, lead_id: int) -> str | None:
    """Which of the organization's numbers a lead is called from: always the same one
    for the same lead, so a person who is tried again sees a number they have seen."""
    numbers = settings.twilio_from_numbers or ([settings.twilio_from_number] if settings.twilio_from_number else [])
    return numbers[lead_id % len(numbers)] if numbers else None


def describe(org: Organization, settings: Settings) -> dict[str, Any]:
    """An organization's settings as shown to its people. The webhook secret is never shown."""
    own = of(org)
    return {
        "from_numbers": own.from_numbers or ([settings.twilio_from_number] if is_default(org) and settings.twilio_from_number else []),
        "human_transfer_number": own.human_transfer_number or (settings.human_transfer_number if is_default(org) else None),
        "booking_webhook_url": own.booking_webhook_url or (settings.booking_webhook_url if is_default(org) else None),
        "booking_webhook_signed": bool(own.booking_webhook_secret if own.booking_webhook_url else (settings.booking_webhook_secret if is_default(org) else None)),
        "cost_per_connected_minute_usd": own.cost_per_connected_minute_usd if own.cost_per_connected_minute_usd is not None else settings.cost_per_connected_minute_usd,
        "cost_per_call_attempt_usd": own.cost_per_call_attempt_usd if own.cost_per_call_attempt_usd is not None else settings.cost_per_call_attempt_usd,
        "can_place_calls": bool(own.from_numbers or (is_default(org) and settings.twilio_from_number)),
    }


def _save(org: Organization, new: OrganizationSettings) -> None:
    org.settings_json = new.model_dump(exclude_none=True)


def set_by_admin(org: Organization, *, human_transfer_number: str | None, booking_webhook_url: str | None, booking_webhook_secret: str | None, keep_secret: bool) -> None:
    """What an organization's own admin decides. ``keep_secret``: the secret was not sent, leave it as it is."""
    current = of(org)
    try:
        new = OrganizationSettings(**{**current.model_dump(), "human_transfer_number": human_transfer_number, "booking_webhook_url": booking_webhook_url,
                                      "booking_webhook_secret": current.booking_webhook_secret if keep_secret else (booking_webhook_secret or None)})
    except ValueError as e:
        raise SettingsError(_first_problem(e)) from None
    if new.booking_webhook_url is None:
        new.booking_webhook_secret = None
    _save(org, new)


def set_by_operator(s: Session, org: Organization, *, from_numbers: list[str] | None = None, cost_per_connected_minute_usd: float | None = None,
                    cost_per_call_attempt_usd: float | None = None, clear_rates: bool = False) -> None:
    """What the deployment's operator assigns: caller numbers and rates."""
    current = of(org)
    values = current.model_dump()
    if from_numbers is not None:
        values["from_numbers"] = from_numbers
    if clear_rates:
        values["cost_per_connected_minute_usd"] = values["cost_per_call_attempt_usd"] = None
    if cost_per_connected_minute_usd is not None:
        values["cost_per_connected_minute_usd"] = cost_per_connected_minute_usd
    if cost_per_call_attempt_usd is not None:
        values["cost_per_call_attempt_usd"] = cost_per_call_attempt_usd
    try:
        new = OrganizationSettings(**values)
    except ValueError as e:
        raise SettingsError(_first_problem(e)) from None
    # A number belongs to one organization: two customers must never call from the same one.
    for other in s.execute(select(Organization).where(Organization.id != org.id)).scalars():
        taken = set(of(other).from_numbers) & set(new.from_numbers)
        if taken:
            raise SettingsError(f"{sorted(taken)[0]} is already assigned to another organization")
    _save(org, new)


def _first_problem(error: ValueError) -> str:
    problems = getattr(error, "errors", None)
    if callable(problems):
        first = problems()[0]
        return str(first.get("msg", error)).removeprefix("Value error, ")
    return str(error)
