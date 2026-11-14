"""Requests this server sends to an address a customer typed in.

The address is chosen by someone outside, and this server sits inside a network that
holds a database and, on a cloud machine, a metadata service. So before anything is
sent the name is resolved and every address it has must be public, and the connection
then goes to the address that was checked, not to whatever the name answers a moment
later. Redirects are not followed.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit

import httpx

from .training.ingest import IngestError
from .training.web import _checked, pinned_request

USER_AGENT = "DevyTalk-Webhooks/1.0"


class AddressError(ValueError):
    """The address cannot be used. The message is for the person who typed it."""


class DeliveryError(RuntimeError):
    """The request could not be made. The message is safe to store and to show."""


def check_address(url: str) -> str:
    """Whether a customer may register this address at all: https, a host name, the
    standard port, no credentials in it. Returns it trimmed. What the name resolves to
    is checked when something is sent, every time."""
    url = (url or "").strip()
    if len(url) > 500:
        raise AddressError("the address is longer than 500 characters")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise AddressError("the address must start with https://")
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        raise AddressError("this is not a valid address")
    try:
        port = parts.port
    except ValueError:
        raise AddressError("this is not a valid address") from None
    if port not in (None, 443):
        raise AddressError("only the standard https port (443) can be used")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise AddressError(f"'{host}' is not a public address")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise AddressError(f"'{host}' is not a public address") from None
    else:
        if not literal.is_global or literal.is_multicast:
            raise AddressError(f"'{host}' is not a public address")
    return url


async def post(url: str, body: bytes, headers: dict[str, str], *, timeout: float, public_only: bool = True) -> int:
    """POST ``body`` and return the status the receiver answered with.

    ``public_only`` (the default) refuses a name that resolves to anything but public
    addresses. It is switched off only for an address the deployment's own operator
    configured, who may point at a system inside their network on purpose."""
    target, extra, extensions = url, {}, {}
    if public_only:
        try:
            checked, address = await asyncio.to_thread(_checked, url)
        except IngestError as e:
            raise DeliveryError(str(e)) from None
        target, extra, extensions = pinned_request(checked, address)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.post(target, content=body, headers={**headers, **extra}, **({"extensions": extensions} if extensions else {}))
    except httpx.TimeoutException:
        raise DeliveryError(f"no answer within {timeout:g} seconds") from None
    except httpx.HTTPError as e:
        raise DeliveryError(f"the address could not be reached ({type(e).__name__})") from None
    return response.status_code
