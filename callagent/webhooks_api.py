"""HTTP API for an organization's webhook endpoints. For its admins."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import webhooks
from .apideps import acting_as, organization_id, require_api_token
from .db import WebhookDelivery, run_db

router = APIRouter(prefix="/api/webhooks", dependencies=[Depends(require_api_token)], tags=["webhooks"])


class EndpointIn(BaseModel):
    url: str = Field(max_length=500, description="Where events are sent. https only, a public address.")
    events: list[str] = Field(description='Which events, e.g. ["call.finalized", "appointment.booked"]; ["*"] for all of them.')
    description: str = Field(default="", max_length=200)


class EndpointPatch(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    events: list[str] | None = None
    description: str | None = Field(default=None, max_length=200)
    active: bool | None = Field(default=None, description="false switches the endpoint off; true switches it on again and forgets earlier failures.")


def _endpoint(s, endpoint_id: int):
    endpoint = webhooks.get_endpoint(s, organization_id(), endpoint_id)
    if endpoint is None:
        raise HTTPException(404, "no such endpoint")
    return endpoint


@router.get("")
async def list_endpoints():
    """The organization's endpoints, and the events there are to choose from. Secrets are never listed."""
    return await run_db(lambda s: {"events": list(webhooks.EVENT_NAMES), "endpoints": [webhooks.describe(e) for e in webhooks.list_endpoints(s, organization_id())]})


@router.post("", status_code=201)
async def create_endpoint(body: EndpointIn, request: Request):
    """Register an address. The answer carries the signing secret, this once."""

    def _create(s):
        try:
            endpoint, secret = webhooks.create_endpoint(s, organization_id(), body.url, body.events, body.description, acting_as(request, "api token"))
        except webhooks.WebhookError as e:
            raise HTTPException(400, str(e)) from None
        return {**webhooks.describe(endpoint), "secret": secret}

    return await run_db(_create)


@router.patch("/{endpoint_id}")
async def update_endpoint(endpoint_id: int, body: EndpointPatch):
    def _update(s):
        endpoint = _endpoint(s, endpoint_id)
        try:
            webhooks.update_endpoint(endpoint, url=body.url, events=body.events, description=body.description, active=body.active)
        except webhooks.WebhookError as e:
            raise HTTPException(400, str(e)) from None
        return webhooks.describe(endpoint)

    return await run_db(_update)


@router.post("/{endpoint_id}/secret")
async def rotate_secret(endpoint_id: int):
    """Replace the signing secret. The new one is in the answer, this once; the old one stops working at once."""

    def _rotate(s):
        endpoint = _endpoint(s, endpoint_id)
        secret = webhooks.rotate_secret(endpoint)
        return {**webhooks.describe(endpoint), "secret": secret}

    return await run_db(_rotate)


@router.delete("/{endpoint_id}")
async def delete_endpoint(endpoint_id: int):
    def _delete(s):
        endpoint = _endpoint(s, endpoint_id)
        described = webhooks.describe(endpoint)
        webhooks.delete_endpoint(s, endpoint)
        return described

    return await run_db(_delete)


@router.get("/{endpoint_id}/deliveries")
async def list_deliveries(endpoint_id: int, limit: int = 50, before_id: int | None = None):
    """What was sent to an endpoint, newest first: whether it arrived, after how many attempts, and the answer."""
    return await run_db(lambda s: [webhooks.describe_delivery(d) for d in webhooks.list_deliveries(s, _endpoint(s, endpoint_id), limit, before_id)])


@router.post("/{endpoint_id}/test")
async def send_test(endpoint_id: int):
    """Send a `ping` to the endpoint now. The answer says whether it arrived."""
    result = await webhooks.send_test(organization_id(), endpoint_id)
    if result is None:
        raise HTTPException(404, "no such endpoint")
    return result


@router.post("/{endpoint_id}/deliveries/{delivery_id}/redeliver")
async def redeliver(endpoint_id: int, delivery_id: int):
    """Try a delivery again that was given up on. The receiver sees the same event id."""

    def _again(s):
        endpoint = _endpoint(s, endpoint_id)
        delivery = s.get(WebhookDelivery, delivery_id)
        if delivery is None or delivery.endpoint_id != endpoint.id:
            raise HTTPException(404, "no such delivery")
        try:
            return webhooks.describe_delivery(webhooks.redeliver(s, delivery))
        except webhooks.WebhookError as e:
            raise HTTPException(400, str(e)) from None

    return await run_db(_again)
