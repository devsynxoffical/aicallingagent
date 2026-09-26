"""Thin wrapper over Twilio for outbound dialing, SMS and in-call transfers."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Dial, VoiceResponse

from ..config import Settings, get_settings


@lru_cache
def twilio_client(account_sid: str, auth_token: str) -> Client:
    return Client(account_sid, auth_token)


def _client(settings: Settings) -> Client:
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set")
    return twilio_client(settings.twilio_account_sid, settings.twilio_auth_token)


def place_call(call_run_id: int, to_number: str, settings: Settings | None = None) -> str:
    """Start an outbound call. Twilio fetches TwiML from our /twilio/voice endpoint once
    answered (with AnsweredBy from answering-machine detection) and reports lifecycle
    events to /twilio/status. Returns the Call SID."""
    settings = settings or get_settings()
    if not settings.public_base_url or not settings.twilio_from_number:
        raise RuntimeError("PUBLIC_BASE_URL and TWILIO_FROM_NUMBER must be set")
    base = settings.public_base_url
    call = _client(settings).calls.create(
        to=to_number,
        from_=settings.twilio_from_number,
        url=f"{base}/twilio/voice?call_run_id={call_run_id}",
        method="POST",
        status_callback=f"{base}/twilio/status?call_run_id={call_run_id}",
        status_callback_method="POST",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        machine_detection="DetectMessageEnd",
        machine_detection_timeout=30,
        time_limit=settings.call_time_limit_seconds,
        record=settings.record_calls,
        timeout=25,
    )
    return call.sid


async def place_call_async(call_run_id: int, to_number: str, settings: Settings | None = None) -> str:
    return await asyncio.to_thread(place_call, call_run_id, to_number, settings)


def stream_twiml(call_run_id: int, mode: str, settings: Settings) -> str:
    """TwiML that connects the call's audio to our WebSocket media stream."""
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=settings.ws_url)
    stream.parameter(name="call_run_id", value=str(call_run_id))
    stream.parameter(name="mode", value=mode)
    response.append(connect)
    return str(response)


def hangup_twiml() -> str:
    response = VoiceResponse()
    response.hangup()
    return str(response)


def transfer_call(call_sid: str, to_number: str, settings: Settings | None = None) -> None:
    """Redirect a live call to a human. This ends our media stream."""
    settings = settings or get_settings()
    response = VoiceResponse()
    dial = Dial(caller_id=settings.twilio_from_number)
    dial.number(to_number)
    response.append(dial)
    _client(settings).calls(call_sid).update(twiml=str(response))


def send_sms(to_number: str, body: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    msg = _client(settings).messages.create(to=to_number, from_=settings.twilio_from_number, body=body)
    return msg.sid


def hangup_call(call_sid: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    _client(settings).calls(call_sid).update(status="completed")


def validate_signature(settings: Settings, url: str, form: dict[str, Any], signature: str | None) -> bool:
    if not settings.twilio_validate_signature:
        return True
    if not (settings.twilio_auth_token and signature):
        return False
    return RequestValidator(settings.twilio_auth_token).validate(url, form, signature)
