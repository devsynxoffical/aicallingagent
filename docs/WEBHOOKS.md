# Webhooks: telling your own systems what happened

An organization registers addresses of its own systems (a CRM, a calendar, a data
pipeline) and chooses which events each one receives. This is how results leave
DevyTalk without anyone polling for them.

**Not validated against a real receiver.** The tests use a stand-in for the
receiving end; no delivery has been made over a network from this code.

## Events

| Event | When | `data` |
| --- | --- | --- |
| `call.initiated` | A call was created for a lead | `lead_id`, `manual`, `playbook_version` |
| `call.answered` | The audio connection opened | `direction` |
| `call.completed` | The phone provider reported the end of a connected call | `status`, `seconds`, `direction` |
| `call.failed` | The call ended without connecting: busy, no answer, failed | `status`, `seconds`, `direction` |
| `call.finalized` | The call is over and its outcome is known | `disposition`, `reviewed`, `direction` |
| `call.transferred` | The provider accepted a transfer to a person | |
| `appointment.booked` | The agent booked a meeting | `starts_at`, `lead_id` |
| `campaign.started`, `campaign.paused` | Someone started or paused a campaign | `by` |
| `campaign.stopped` | The dialer stopped it: budget, schedule, a rule | `status`, `reason` |
| `playbook.approved` | A playbook version went live | `version`, `by`, `source` |
| `ping` | You pressed "Send test" | a message |

`["*"]` subscribes an endpoint to all of them. `call.finalized` is the one a CRM
usually wants: it carries the outcome and, when the call was reviewed, a summary.

## What arrives

A `POST` with a JSON body:

```json
{
  "id": "evt_4821",
  "type": "call.finalized",
  "api_version": 1,
  "created_at": "2026-11-14T16:02:11+00:00",
  "organization_id": 2,
  "campaign": {"id": 7, "name": "Bolt outreach"},
  "data": {"disposition": "meeting_booked", "reviewed": true, "direction": "outbound"},
  "call": {"id": 913, "direction": "outbound", "status": "completed", "outcome": "meeting_booked",
           "from_number": "+14155550210", "started_at": "…", "ended_at": "…", "duration_seconds": 184,
           "language": "en", "cost_usd": 0.42, "summary": "…", "next_action": "…", "interest_level": 8},
  "lead": {"id": 55, "phone": "+14155550299", "first_name": "Mina", "last_name": "", "company": "Harbor Cafe",
           "email": "", "status": "completed", "score": 81}
}
```

`appointment.booked` adds `"appointment": {"id", "starts_at", "timezone", "notes"}`.

- `data` is the fact as it was recorded. `call`, `lead` and `appointment` are those
  things **as they are when the delivery is sent**, which for a retry may be later
  than the event.
- What was said on a call is not sent. A system that may read it asks the API
  (`GET /api/calls/{id}`) with an API key.
- After a person is erased, their details are no longer in later deliveries.

Headers:

| Header | Value |
| --- | --- |
| `X-Callagent-Event` | the event's name |
| `X-Callagent-Delivery` | `whd_<n>`: the same on every retry of this delivery |
| `X-Callagent-Timestamp` | seconds since 1970 when this attempt was sent |
| `X-Callagent-Signature` | `t=<seconds>,v1=<hex>` |

## Checking the signature

`v1` is HMAC-SHA256, under the endpoint's secret, of the timestamp, a dot, and the
exact bytes of the body. Check it before parsing the JSON, and refuse a timestamp
more than five minutes from your clock: that is a recording of a real delivery sent
again.

```python
import hashlib, hmac, time

def is_ours(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    if not parts.get("t", "").isdigit() or abs(time.time() - int(parts["t"])) > tolerance:
        return False
    expected = hmac.new(secret.encode(), parts["t"].encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))
```

```js
import crypto from "node:crypto";

export function isOurs(secret, rawBody, header, tolerance = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  if (!/^\d+$/.test(parts.t ?? "") || Math.abs(Date.now() / 1000 - Number(parts.t)) > tolerance) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${parts.t}.`).update(rawBody).digest("hex");
  const given = Buffer.from(parts.v1 ?? "", "utf8");
  return given.length === expected.length && crypto.timingSafeEqual(given, Buffer.from(expected, "utf8"));
}
```

The secret (`whsec_…`) is shown once, when the endpoint is created or its secret is
replaced. Each endpoint has its own.

## What a receiver must do

1. Answer `2xx` within 10 seconds. Do the work afterwards.
2. Ignore an `id` it has already handled: a delivery can arrive more than once.
3. Not rely on order: `call.finalized` can arrive before `call.completed`.

## Retries and switching off

Anything but a `2xx` in time is a failure: another status, a redirect (not
followed), no answer, an address that cannot be reached. A failed delivery is tried
again after 30 seconds, 1 minute, 2 minutes and so on, eight attempts over about an
hour, and is then marked failed. It can be sent again by hand.

An endpoint with 15 failed deliveries in a row is switched off and says why. Events
that happen while it is off are not kept for it. An admin switches it on again.

The record of deliveries is kept for 30 days.

## Limits and rules

- `https` only, port 443, a host name that resolves to public addresses. The check
  is made every time something is sent, and the connection goes to the address that
  was checked. An address inside this deployment's network is never contacted.
- 10 endpoints per organization.
- Endpoints are managed by the organization's admins in the console, or with an
  admin's session. **An API key cannot manage endpoints**, whatever its role: a key
  that leaks must not be able to have the organization's events sent elsewhere.
- Deliveries wait in their own queue (`integrations`): a slow endpoint delays other
  deliveries, never calls or booking notifications.

## API

| Method and path | What |
| --- | --- |
| `GET /api/webhooks` | Endpoints, and the list of events |
| `POST /api/webhooks` | `{url, events, description}` → the endpoint with its `secret`, once |
| `PATCH /api/webhooks/{id}` | Change `url`, `events`, `description`, `active` |
| `POST /api/webhooks/{id}/secret` | Replace the secret → the new one, once |
| `DELETE /api/webhooks/{id}` | Remove the endpoint and the record of its deliveries |
| `GET /api/webhooks/{id}/deliveries` | What was sent, newest first |
| `POST /api/webhooks/{id}/test` | Send a `ping` now → how it went |
| `POST /api/webhooks/{id}/deliveries/{delivery}/redeliver` | Try a failed delivery again |

## The booking webhook

The older single booking webhook (`PUT /api/organization/settings`,
`booking_webhook_url`) still works and is unchanged for the receiver: one
`appointment.booked` report per booking, signed over the body alone. An address an
organization's admin typed in must now be public, like an endpoint's; the one in
the deployment's own environment is used as its operator set it. New integrations
should register an endpoint for `appointment.booked` instead.
