# Security review

State of the code on this branch on 2026-11-14. Reviewed by reading the code and by
the automated tests named below. **Not done: a penetration test, a review by anyone
outside this work, a dependency scan with results read, and any test against a real
provider or a deployed instance.** Ids such as A-10 refer to
`ENTERPRISE_BACKEND_AUDIT.md`.

## Summary

Several customer organizations can share one deployment on PostgreSQL. A request is
confined to the organization of the account that makes it, by the application on
every route and again by PostgreSQL row-level security. People sign in with their
own accounts and one of three roles, checked on the server on every route. Every
attempt to change something is recorded with who made it. Logs carry no phone
numbers or email addresses. Backups can be encrypted.

What still stands between this and selling to a second customer:

1. **No second factor, and no email.** Someone who forgot their password asks an
   admin for a one-time reset link; an organization whose only admin is locked out
   asks the deployment's operator (A-13).
2. **No legal review.** Consent, do-not-call lists, erasure and retention are
   mechanisms an organization configures (`COMPLIANCE.md`); whether a configuration
   satisfies the law of a market has not been checked by anyone qualified.
3. **One phone provider account serves every organization.** Each organization has
   its own caller numbers, assigned by the operator, but they are all numbers of
   the deployment's account.
4. **Nothing has been attacked on purpose.**

## By area

| Area | State | Evidence or detail |
| --- | --- | --- |
| Authentication | Built | Passwords hashed with scrypt and a per-password salt; session tokens are random, stored only as a SHA-256, expire after 12 hours, and end at sign-out or deactivation; the same answer for an unknown email, a wrong password and a deactivated account. `tests/test_accounts.py` |
| Joining and resetting | Built | A person joins by a one-time link an admin makes and hands over; they choose their own password, so no admin knows another person's. The same kind of link resets a password, and using it ends every sign-in of that account. 256 random bits, stored as a SHA-256; works once; a week to join, a day to reset; withdrawn by an admin, by a newer link for the same person, or by deactivating the account. An unknown, used, withdrawn and expired link get the same answer. A key cannot make one. The link is a bearer credential while it lasts: whoever is handed it gets the account. `tests/test_invitations.py` |
| Brute force | Partly | An account locks for 15 minutes after 5 wrong passwords. The console limits sign-in attempts per address, keyed on the address its own proxy reports, not one the client can choose. The engine has no limit per address (A-12), so someone can lock a known account out repeatedly |
| Roles | Built | viewer, operator, admin, decided on the server from the declared route; the console's hiding of buttons is a convenience only. A test calls every route as every role |
| Organization isolation | Built, two layers | Application: every lookup goes through the caller's organization; another organization's object answers 404. Database: row-level security on 21 tables, forced on the owner too, set per transaction. `tests/test_tenancy.py` calls every addressable route as another organization, on SQLite and on PostgreSQL as an ordinary role; `tests/test_row_level_security.py` runs unfiltered queries against the policies |
| Row-level security in a deployment | **Depends on the database role** | A superuser or a role with BYPASSRLS is not bound by the policies. `/api/status` reports `enforced`, `bypassed` or `missing`; run the engine as an ordinary role |
| The operator's token | By design | One static secret for the deployment's operator, compared in constant time. It creates organizations, assigns caller numbers and rates, corrects the ledger, reads metrics, and acts in any organization it names. Set it explicitly, keep it in a secret store, rotate by changing it and restarting |
| API keys | Built | Per organization, with a role; only a SHA-256 is stored and the key is shown once; revocable at once. A key cannot create keys, manage people or change a password, whatever its role, so a leaked key cannot keep itself alive. What a key does is in the audit trail under its name |
| Caller numbers | Built | Assigned to an organization by the operator only: an organization cannot type a caller number of its choosing. A number belongs to one organization. An organization without a number cannot place calls, and never falls back to another's |
| Booking webhook secret | Built | Per organization, write-only: never returned by the API. Stored in the database in the clear, as are webhook endpoints' secrets |
| Provider webhooks | Built | The phone provider's signature is checked on every webhook, against the public URL. Switching the check off (`TWILIO_VALIDATE_SIGNATURE=false`) is logged as an error at start; it is still possible |
| Webhook replay | Mitigated | The provider's signatures carry no timestamp, so a captured request can be sent again. A replay changes nothing: statuses only move forward, the ledger has one line per key, inbound calls are unique per provider call id |
| Media WebSocket | Built | Public endpoint; needs the per-call random token from our own instructions to the provider; the claim is a single conditional update, so exactly one connection gets a call; a connection that does not complete its opening exchange in 10 seconds is dropped |
| In-call actions | Built | The agent can only use the tools its campaign enables, checked on the server; bookings, callbacks and texts are bounded; links in texts only to hosts in the approved playbook; a transfer is recorded only when the provider accepted it. `tests/test_call_safety.py` |
| Prompt injection | Reduced, not removed | Document passages, lead details and transcripts are marked as data with a rule that they contain no instructions; tools act only on the current lead and within the limits above; nothing the model writes goes live without a person's approval. A document can still state a false fact that the agent repeats; the review of each call lists statements the playbook does not support |
| Outgoing booking webhook | Built | Signed with HMAC-SHA256 when a secret is set, with a delivery id that is the same on every retry. Unsigned when no secret is configured. An address an organization's admin typed in is sent to only if it resolves to public addresses (see the next row); the one in the deployment's environment is the operator's own |
| Webhook endpoints (SSRF, data leaving) | Built | An organization's admin chooses where its events are sent, so the address is hostile input: https and port 443 only; the name is resolved on every send and refused unless every address is public; the connection goes to the address that was checked; redirects are not followed; the response body is never read or stored. Each endpoint has its own secret, shown once, stored readable in the database. Signed over the time and the body, so a receiver can refuse a replay. An API key cannot manage endpoints. `tests/test_webhooks.py` |
| Website reading (SSRF) | Built | http(s) only; every hop resolved and refused unless public; the connection goes to the address that was checked, so a second DNS answer cannot redirect it; size and redirect limits |
| File uploads | Built | 25 MB; type checked against content; Office files refused when they unpack to more than 300 MB; PDFs over 600 pages refused; stored under a generated name outside any served path |
| Request size | Built | 30 MB for any request body to the engine, refused before it is read; 1 MB for JSON through the console |
| Input validation | Built | Request bodies validated by schema; phone numbers normalised; numbers in the AI reviewer's answers range-checked |
| SQL injection | No finding | Queries go through SQLAlchemy with bound parameters; migration and backup code build SQL from constants only |
| Secrets | Acceptable | Read from the environment; never returned by the API; not in backups (tested); the database password goes to backup tools in the environment, not the command line. The API token may be generated into a file with mode 600: set it explicitly in a deployment |
| Personal data in logs | Built | Phone numbers and email addresses masked on every line including exception text; the voice library held at warning so prompts and spoken sentences are not logged; request bodies never logged. `tests/test_observability.py` |
| Audit trail | Built | Every attempt to change something through the API, refused ones included, and sign-ins on known accounts; readable by the organization's admins only; request bodies not stored. Rows are append-only in the code, not in the database |
| Metrics | Built | Operator only; no label identifies a person, a number or an organization |
| Public endpoints | Low | `/health`, `/health/live` and `/health/ready` say nothing about configuration. `/docs` and `/openapi.json` are public and describe the API's shape |
| CSV export | Built | Cells that a spreadsheet would run as formulas are neutralised |
| Backups | Built | Optional AES-256-GCM encryption with tamper detection; without a key they are written in the clear and the command says so. The key is the operator's to keep safe |
| Console (browser side) | Partly | Session in an httpOnly SameSite=Strict cookie, never visible to page scripts; all engine access through a server-side proxy limited to `/api/*`; cross-site changes refused; security headers on every response including a content policy that refuses other origins, framing and plugins. The policy allows inline scripts (the framework needs them without per-request nonces), so it does not stop a script injected into a page. Open (E-8): signing in with the engine's token stores that non-expiring token in the cookie; the engine's error text reaches the browser unchanged |
| Data retention and erasure | Built | A person is erased by phone number (details, call content, website messages, stored notifications), by an admin; call content is removed after the organization's retention period. Recordings at the phone provider and old backups are not reached. See `COMPLIANCE.md` |
| Recording and consent | **Needs a decision per market** (B-13) | The opening can disclose the AI and the recording; what is required where is a legal question |
| Dependencies | Open | Lower-bound versions, no lock file. CI runs a vulnerability scan as a warning, not a gate; nobody has read its output yet |
| Container | Built | Runs as an ordinary user (checked in CI, where the image is built), with a health check that has not been seen to pass yet |
| Transport | Deployment concern | The server speaks plain HTTP; TLS must be terminated in front of it. The phone provider requires HTTPS and WSS |
| Several engine processes | Built | The database decides who dials (a lease), who gets a lead, a call and a job |

## Checked by tests

| Property | Test |
| --- | --- |
| No route gives one organization another's data, and nothing of the other's changes | `test_every_route_refuses_another_organizations_identifiers` (sqlite, postgres) |
| The database itself confines an unfiltered query | `tests/test_row_level_security.py` |
| Each role can do what it is for and nothing more | `test_each_role_can_do_what_it_is_for_and_nothing_more` |
| The organization comes from the account, never from the request | `test_the_organization_comes_from_the_account_never_from_the_request` |
| A refused attempt is recorded with who made it; no request body is kept | `test_every_attempt_to_change_something_is_recorded_with_who_and_how_it_ended` |
| No phone number or email address reaches the logs | `test_phone_numbers_and_email_addresses_never_reach_the_logs` |
| A tool the campaign did not enable is refused even if the model names it | `test_a_tool_the_campaign_did_not_enable_is_refused_even_if_the_model_names_it` |
| A page is fetched from the address that was checked | `test_a_page_is_fetched_from_the_address_that_was_checked` |
| Of many connections with one valid token, one gets the call | `test_of_many_sockets_presenting_the_same_valid_token_exactly_one_gets_the_call` |
| An encrypted backup shows nothing without its key and refuses tampering | `tests/test_backup.py` |
| The ledger is corrected only by the operator, and not across organizations | `test_the_ledger_is_read_by_its_organization_and_corrected_only_by_the_operator` |
| The limiter's key cannot be chosen by the client; headers are set | console `src/lib/rate-limit.test.ts` |
| A name that resolves inside the network is never connected to; a public one is connected to at the address that was checked | `test_a_name_that_resolves_inside_the_network_is_never_connected_to`, `test_a_public_name_is_connected_to_at_the_address_that_was_checked` |
| A link works once; a reset ends every sign-in; a withdrawn, replaced or expired link opens nothing | `tests/test_invitations.py` |
| The routes open without signing in are exactly: sign-in, and opening and using a one-time link | `test_every_api_route_needs_a_sign_in_except_signing_in_itself` |
| An event goes only to endpoints of its own organization, signed with that endpoint's secret | `test_an_event_goes_once_signed_to_the_endpoints_of_its_organization_that_asked_for_it` |

## Before a second customer

1. The engine running as an ordinary database role, with `/api/status` showing
   row-level security `enforced`.
2. The questions in `COMPLIANCE.md`, section 4, answered for the market by someone qualified.
3. An outside security review, and the dependency scan read and acted on.

## Smaller items

A limit on sign-in attempts per address on the engine (A-12); a
second factor and a reset the person can start themselves, by email (A-13); the API description behind sign-in; a lock file and the scan
as a gate; database permissions that forbid changing audit and ledger rows; a
session for the engine token instead of the token itself in the console's cookie.
