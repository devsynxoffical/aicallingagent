# Security review

Reviewed 2026-11-13 by reading the code; no penetration test, no dependency scan and no
test against a real provider were performed. Status reflects the code on this branch.

## Summary

The service is safe to run for **one organization** behind TLS with its API token kept
secret. It is **not** safe to put two customers on one deployment: there is one shared
token and no request is scoped by organization. That is the most important open item
and it blocks the acceptance criterion "no cross-organization data access in security
tests": those tests do not exist yet because the isolation they would test does not
exist yet.

## Review by area

| Area | State | Detail | Audit |
| --- | --- | --- | --- |
| Authentication | Weak by design | One static bearer token for all of `/api/*`, compared in constant time. No users, expiry or rotation; `approved_by` is free text from the client | S-1 |
| Roles | None | Whoever holds the token can do everything | S-1 |
| Organization isolation | **Not enforced** | Campaigns, calls and leads are looked up by global name or id. `GET /api/calls/{id}` returns any call. Leads, calls, appointments and the do-not-call list carry no organization column | S-1, D-3 |
| Provider webhooks | Good | Twilio's signature is checked on every webhook, on by default, against the public URL. The load test signs its requests instead of switching the check off | |
| Webhook replay | Mitigated | Twilio signatures have no timestamp, so a captured request can be sent again. Replays now change nothing: status goes through the state machine, usage has one row per call, inbound calls are unique per provider call id | S-3 |
| Signature check can be disabled | Open | `TWILIO_VALIDATE_SIGNATURE=false` is accepted silently, also in production | S-3 |
| Media WebSocket | Good, one gap | Public endpoint; requires the per-call random token from our own TwiML, a call that is not finished and a matching call id. The single-use check is not atomic | C-8 |
| Outgoing booking webhook | Improved | Signed with HMAC-SHA256 when `BOOKING_WEBHOOK_SECRET` is set; stable delivery id for de-duplication. Unsigned if no secret is configured | C-9 |
| Dashboard | Good | Token checked server-side, kept in an httpOnly SameSite=Strict cookie, never sent to page scripts; all engine access through a server-side proxy limited to `/api/*`; cross-site changes refused (see the `devytalk` repository's tests) | |
| Rate limiting | None on the engine | The dashboard limits sign-in attempts; the engine's API has no limits | S-2 |
| Input validation | Mostly good | Request bodies are validated by schema; phone numbers normalized; lead upload has **no size limit** | S-2 |
| File uploads | Mostly good | Type checked against content, 25 MB limit, stored under a generated name outside any served path. Word and PowerPoint files have no decompressed-size limit | S-6 |
| Website crawling (SSRF) | Mostly good | http(s) only; every hop resolved and refused unless the address is public; size and redirect limits. The address is resolved again by the HTTP client after the check (DNS rebinding) | S-5 |
| SQL injection | No finding | All queries go through SQLAlchemy with bound parameters. Migration and backup code build SQL only from constants |
| Secrets | Acceptable | Read from the environment. Never returned by the API. Not in backups (tested). The PostgreSQL password is passed to backup tools in the environment, not on the command line. The API token may be auto-generated into a file with mode 600; set it explicitly in production | |
| Secrets and personal data in logs | Open | Caller numbers are logged for inbound calls; provider errors can contain numbers. No redaction | S-4 |
| Prompt injection | Open | Text from uploaded documents and crawled websites is returned to the model next to tools that send texts, transfer calls and book meetings. Tools act only on the current lead, which limits the damage | AI-3 |
| CSV export | Open | Cells are not protected against spreadsheet formulas | S-7 |
| Audit log | None | State-changing actions are not recorded with who did them | S-8 |
| Data retention and erasure | None | Transcripts and lead data are kept forever; no erase operation | S-8 |
| Database access | Acceptable | Foreign keys enforced; uniqueness enforced; least-privilege database roles and row-level security not set up | D-3 |
| Dependencies | Open | Lower-bound versions, no lock file, no vulnerability scan; the container runs as root | S-9 |
| Transport | Deployment concern | The server speaks plain HTTP; TLS must be terminated in front of it. Twilio requires HTTPS/WSS for webhooks and media |

## Fixed in this round

- Double dialing and dialing a suppressed number through a race (C-1).
- Duplicate call records from a webhook delivered twice (D-2).
- Replayed status callbacks double-counting usage (B-1, S-3).
- Booking webhooks: unsigned, unretried, blocking the live call (C-9).
- Restore refuses a tampered backup; backups contain no secrets (D-8).

## What has to happen before a second customer

1. API keys stored hashed, each belonging to one organization and one role
   (owner, operator, read-only). The existing token becomes the default
   organization's owner key so current installs keep working.
2. `organization_id` on leads, calls, appointments and the do-not-call list; campaign
   names unique per organization; every query filtered by the caller's organization;
   objects of another organization answer 404, not 403.
3. A test suite that creates two organizations and tries every route of one with the
   other's key. This is the acceptance test for isolation.
4. On PostgreSQL, row-level security policies as a second line of defence.
5. An audit log of approvals, rollbacks, campaign starts and exports, with the key
   that made them.

## Smaller items, in order

Rate limits and an upload size cap (S-2); log redaction (S-4); refuse to start with
signature checking off outside development (S-3); decompressed-size limit (S-6);
pin the crawler's connection to the checked address (S-5); formula-safe CSV (S-7);
lock file, non-root container and a scan step (S-9); atomic WebSocket claim (C-8);
tool limits and untrusted-content marking for retrieved text (AI-3).
