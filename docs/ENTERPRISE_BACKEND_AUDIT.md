# Enterprise backend audit

Scope: the calling engine (`aicallingagent`, package `callagent/`) and the server-side
parts of the console (`devytalk/apps/web`). Every finding below was read from the code
at the commit this file was written against, not from earlier documents; file and line
references are to that commit and will drift.

How to read a finding:

- **Now**: what the code does. **Weakness / cause**: why that is a problem and where
  it comes from. **Scenario**: a concrete way it fails. **Impact**: what it costs the
  business. **Fix**: the recommended change. **Size**: S (hours), M (a day or two),
  L (several days). **Needs**: what must exist first. **Done when**: the acceptance
  criterion, written so a test can check it.
- **Status** is kept current as work lands: *open*, *fixed* (with the test that shows
  it), or *accepted* (with the reason).

Priorities: **P0** critical security, data integrity or production reliability.
**P1** major architecture or performance. **P2** scalability and maintainability.
**P3** optimisation and later work.

Earlier audit: `BACKEND_AUDIT.md` (40 findings, most fixed). Findings from it that are
still open are carried over here under their original ids (for example C-8, A-1).

## Summary

| Area | P0 | P1 | P2 | P3 |
| --- | --- | --- | --- | --- |
| A. Organization isolation and access | 6 | 4 | 3 | 1 |

(The remaining areas are added below as their review completes.)

---

## A. Organization isolation and access

The engine was built for one organization. Accounts and roles exist, and several
tables carry an `organization_id`, but no request knows which organization its caller
belongs to, so nothing is filtered by it.

### A-1 (P0) Requests carry no organization

- **Now**: `Principal` (`accounts/__init__.py`) has a role and a user id, no
  organization. `require_api_token` (`apideps.py`) authenticates and checks the role
  only.
- **Weakness / cause**: authorization answers "may this role do this", never "is this
  theirs". The model predates multiple organizations.
- **Scenario**: a viewer of organization B calls any route with an identifier of
  organization A and gets A's data.
- **Impact**: complete loss of confidentiality between customers; blocks selling to a
  second customer.
- **Fix**: put `organization_id` on the principal (from the user's row), make it
  available to every query helper, and make "not yours" indistinguishable from "does
  not exist" (404).
- **Size**: M. **Needs**: nothing.
- **Done when**: a test signs in as a user of organization B and calls every `/api`
  route with organization A's identifiers; every answer is 404 (or an empty list) and
  nothing of A's changes.

### A-2 (P0) Campaigns are looked up globally, by name or by number

- **Now**: `apideps.get_campaign` returns `s.get(Campaign, int(x))` when the path
  segment is all digits, else the campaign with that name, in any organization.
  About 35 routes use it. `campaigns.name` is unique across the whole deployment.
- **Weakness / cause**: one helper, no ownership check; the digit shortcut also makes
  a campaign named "17" resolve to campaign id 17.
- **Scenario**: organization B reads A's leads, transcripts and playbooks, or pauses
  A's dialing, by naming A's campaign. Two customers cannot both have a campaign
  called "Q4".
- **Impact**: cross-customer read and write of everything a campaign owns.
- **Fix**: resolve by `(organization_id, name)` only; drop the numeric shortcut;
  unique index on `(organization_id, name)` instead of `name`.
- **Size**: M. **Needs**: A-1.
- **Done when**: two organizations each have a campaign with the same name and each
  sees only its own; a numeric path segment is treated as a name.

### A-3 (P0) `POST /api/campaigns` overwrites a campaign found by global name

- **Now**: `server.py create_campaign` loads `get_campaign_by_name(body.name)` and
  records the posted playbook as its active version.
- **Scenario**: organization B posts a playbook under a name A already uses and
  replaces what A's agent says on live calls.
- **Impact**: one customer can change another's live sales script.
- **Fix**: same lookup as A-2; a new campaign is created in the caller's organization.
- **Size**: S. **Needs**: A-2.
- **Done when**: the cross-organization test in A-1 covers this route and A's active
  version number is unchanged afterwards.

### A-4 (P0) Rows fetched by primary key without an ownership check

- **Now**: `GET /api/calls/{id}` (`s.get(CallRun, id)`), `POST /api/jobs/{id}/retry`
  (`jobs.retry_dead`), `PATCH /api/users/{id}`, `POST /api/requests/{id}/handled`.
- **Scenario**: counting upwards through call ids returns every customer's
  transcripts, summaries and recording links; an admin of B resets the password of a
  user of A and signs in as them.
- **Impact**: data disclosure and account takeover across customers.
- **Fix**: every fetch by id also filters by the caller's organization.
- **Size**: S each. **Needs**: A-1; `jobs` needs an organization column (A-7).
- **Done when**: covered by the A-1 test for each of these routes.

### A-5 (P0) Lists that span every organization

- **Now**: `GET /api/campaigns`, `/api/timeline`, `/api/jobs`, `/api/requests` and
  `/api/users` select without an organization filter.
- **Scenario**: every customer sees every other customer's campaign names, call
  volumes, team members and failed-job error text.
- **Impact**: disclosure of customer lists and activity.
- **Fix**: filter each by the caller's organization.
- **Size**: S. **Needs**: A-1.
- **Done when**: with two organizations populated, each list returns only the
  caller's rows.

### A-6 (P0) Accounts are always created in the default organization

- **Now**: `accounts.create_user` sets `organization_id=default_organization_id(s)`.
  There is no way to create an organization.
- **Scenario**: a second customer cannot be onboarded at all without sharing the
  first one's organization.
- **Impact**: the product cannot serve more than one customer.
- **Fix**: users are created in the acting admin's organization; organizations are
  created (with their first admin) only by the platform operator.
- **Size**: M. **Needs**: A-1.
- **Done when**: the platform operator creates two organizations with one admin
  each; each admin can add people only to their own.

### A-7 (P1) Tables without an organization of their own

- **Now**: `leads`, `call_runs`, `appointments`, `jobs`, `do_not_call` have no
  `organization_id`; `campaigns.organization_id` and `usage_records.organization_id`
  are nullable. Leads and calls reach their organization only through a join on the
  campaign.
- **Weakness / cause**: filtering the two largest tables by organization needs a
  join every time, row-level security cannot be expressed on them, and a job cannot
  be listed or retried by its owner.
- **Scenario**: a report across an organization's calls joins every call to its
  campaign; a database-level policy cannot protect the tables that hold transcripts.
- **Impact**: slower organization-wide queries and no defence in depth on the most
  sensitive tables.
- **Fix**: add `organization_id` (NOT NULL, indexed, backfilled from the campaign)
  to these tables; fill it in one place when a row is created.
- **Size**: M. **Needs**: nothing.
- **Done when**: the migration backfills an existing database, no row has a null
  organization, and creating any of these rows without one fails.

### A-8 (P1) The do-not-call list is shared by all organizations

- **Now**: `do_not_call.phone_e164` is globally unique; `is_dnc` checks the one list.
- **Scenario**: a person opts out with customer A; customer B's import report and
  dial errors reveal that the number is on a list, and B cannot call someone who
  never opted out of B.
- **Impact**: leaks one customer's opt-outs to another and blocks legitimate calls.
  (An opt-out is owed to the seller that was asked, so per-organization is also the
  legally expected scope.)
- **Fix**: `organization_id` on the list, unique `(organization_id, phone_e164)`.
- **Size**: S. **Needs**: A-7.
- **Done when**: a number suppressed in A is still dialable in B and never dialable
  in A.

### A-9 (P1) Inbound numbers can be claimed by anyone, and a conflict names the owner

- **Now**: `set_inbound_number` accepts any number not already taken and answers 409
  with the other campaign's name; `INBOUND_CAMPAIGN` routes unowned numbers to one
  campaign.
- **Scenario**: organization B learns A's campaign name from the 409, or claims a
  number that A is about to configure.
- **Impact**: minor disclosure; misrouted inbound calls. Proper number ownership
  needs per-organization telephony (A-11).
- **Fix now**: generic 409 across organizations. **Later**: verify ownership against
  the organization's own phone account.
- **Size**: S now. **Needs**: A-1.
- **Done when**: the 409 seen from another organization names nothing.

### A-10 (P1) The engine API token is one secret with full access to everything

- **Now**: `API_TOKEN` (or `DATA_DIR/api_token`) authenticates as an admin with no
  organization.
- **Scenario**: the token leaks from one operator's shell history and every
  customer's data is exposed.
- **Impact**: single point of total compromise.
- **Fix**: treat it as the platform operator's credential: it manages organizations
  and may act inside one it names explicitly; customers never receive it. Per-
  organization API keys (hashed, revocable) for customer automation.
- **Size**: S (operator role) + M (per-organization keys). **Needs**: A-1.
- **Done when**: only the operator credential can create organizations; an
  organization's API key reaches only that organization.

### A-11 (P1) Telephony, booking and transfer settings are per deployment

- **Now**: one Twilio account and caller number, one `BOOKING_WEBHOOK_URL` and
  secret, one `HUMAN_TRANSFER_NUMBER`, one set of cost rates (`config.py`), used for
  every campaign (`voice/tools.py`, `background.py`, `dialer/rules.py`).
- **Scenario**: with two customers, B's booked meetings (lead names, numbers, times)
  are posted to A's webhook and B's prospects are transferred to A's staff.
- **Impact**: personal data sent to the wrong company; wrong caller id; identical
  prices for every customer.
- **Fix**: an organization-settings record (booking webhook and secret, transfer
  number, caller number or phone sub-account, cost rates), with the deployment
  values as defaults for the first organization only.
- **Size**: L. **Needs**: A-1, A-6.
- **Done when**: two organizations with different webhooks each receive only their
  own bookings in a test, and a second organization without settings cannot dial.

### A-12 (P2) Sign-in throttling can be used against a person, and tells accounts apart

- **Now**: five wrong passwords lock that account for 15 minutes and answer 429; an
  unknown email answers 401. There is no per-address limit on the engine.
- **Scenario**: an attacker keeps a known admin locked out, or tests which emails
  have accounts by watching for 429.
- **Fix**: limit by network address as well as by account, and answer the same way
  whether or not the account exists.
- **Size**: S. **Needs**: nothing.
- **Done when**: six wrong passwords for an unknown email and for a real one produce
  the same sequence of answers.

### A-13 (P2) No password reset by email, no second factor, no audit trail of admin actions

- **Now**: passwords are reset by an admin or the command line; nothing records who
  changed a role, deactivated an account or approved a playbook beyond the version's
  `approved_by`.
- **Fix**: an append-only audit log of privileged actions (who, what, when, from
  where); self-service reset and optional TOTP once email sending exists.
- **Size**: M (audit log), L (reset and second factor). **Needs**: A-1.
- **Done when**: every privileged route writes one audit row, readable by the
  organization's admins and by nobody else.

### A-14 (P2) Background work has no organization context or fairness

- **Now**: the dial loop, the job worker and the reconciler work across all rows;
  the reconciler takes the 200 oldest quiet calls deployment-wide.
- **Scenario**: one customer with thousands of stuck calls delays reconciliation for
  everyone else.
- **Fix**: organization on jobs (A-7), per-organization batches and limits.
- **Size**: M. **Needs**: A-7.
- **Done when**: a backlog in one organization does not delay another's jobs in a
  test.

### A-15 (P3) Small disclosures

- `/docs` and `/openapi.json` are public; `/health` lists missing setting names; the
  401 text names the token file; creating a user answers 409 for an email used in
  another organization.
- **Fix**: serve the API description to signed-in callers only; keep `/health` to
  "ok" and move detail to the authenticated status route.
- **Size**: S.
