# Calling people lawfully: what the engine enforces and what it does not decide

**This is not legal advice, and nothing here has been reviewed by a lawyer.** Whether
a call may be made depends on the country (often the state), on who is called
(a business or a consumer), on what is said, and on how the number was obtained.
The engine gives an organization the means to enforce what it has decided. It does
not know what the law requires, and an organization that configures nothing gets no
protection from it.

Before calling in any market, have someone qualified answer the questions in
section 4 for that market.

## 1. What is enforced, once configured

| Mechanism | Where it is decided | What it guarantees | Shown by |
| --- | --- | --- | --- |
| **Do-not-call list**, per organization | Where every outbound call is created | A number on the list is never called, by the dial loop or by hand; it cannot be added to a campaign; importing a list removes its numbers from every call list at once | `tests/test_privacy.py`, `tests/test_tenancy.py` |
| **Opt-out during a call** | The agent's tool; the review after the call | Either puts the number on the list. The person is not called again | `test_an_opt_out_reaches_the_do_not_call_list_however_it_was_noticed` |
| **Consent on record** (`require_consent` in a campaign's rules) | Where every outbound call is created | With the rule on, a person with no ground for calling on record is never called, by the dial loop or by hand. They stay in the list, uncalled | `test_a_campaign_that_requires_consent_never_calls_someone_without_it` |
| **Calling hours and days** | Before every dial | Calls only inside the campaign's window, in the lead's own time zone when it is known | `tests/test_campaign_rules.py` |
| **Attempts** | Before every dial | At most the configured number of attempts per person, spaced by the configured delay | same |
| **Stop on opt-out rate** | Before every dial | A campaign stops itself when too large a share of answered calls ask not to be called | same |
| **AI disclosure** | The playbook | The opening says the caller is an AI when the playbook says to; the agent does not claim to be human when asked | `tests/test_playbook.py` |
| **One organization per caller number** | Assigned by the operator | An organization cannot choose its caller id; two organizations never call from one number | `tests/test_organization_settings.py` |
| **Erasure** | `POST /api/people/erase`, an admin | Everything held about the person is removed (section 3) | `test_erasing_a_person_removes_them_everywhere_and_keeps_what_accounting_needs` |
| **Retention period** | The organization's settings | What was said on a call is removed that many days after it ended | `test_call_content_is_removed_after_the_period_the_organization_set_and_not_before` |
| **Audit trail** | Every change through the API | Who changed a rule, removed a number from the list, or erased a person, and when | `tests/test_observability.py` |

## 2. Recording the ground for a call

A lead can carry `consent_basis` (for example `opt_in`, `existing customer`),
`consent_at` and `consent_source`.

- **Per person**: a sheet with a column named Consent (or Opt-in, Permission), and
  optionally Consent date and Consent source. "yes" means `opt_in`; "no" or empty
  means nothing on record; any other text is kept as the ground.
- **For a whole list**: `POST /api/campaigns/{name}/leads?consent=opt_in&consent_source=…`
  states one ground for everyone in the sheet. A consent column in the sheet wins
  over it.
- With no date given, the moment of the import is recorded.

The engine records what it is told. It cannot check that a person did consent, that
the consent covers automated or AI calls, or that it has not been withdrawn
elsewhere. Keep the evidence (the form, the timestamp, the wording) in the system
that collected it.

## 3. Removing what is held

`POST /api/people/erase` with a phone number, by an admin:

| Data | What happens |
| --- | --- |
| The lead's name, company, email, notes, extra columns, score details, consent record | Emptied |
| The phone number on the lead | Replaced by a placeholder |
| Calls: transcript, tool record, review, recording link | Removed; the call is marked as erased |
| Calls: time, duration, outcome, cost; ledger lines | Kept: accounting needs them and they say nothing about the person |
| Appointment notes | Emptied |
| Messages the person sent through the website | Deleted |
| Stored or queued booking notifications about them | Emptied; a queued one is not sent |
| The do-not-call list | The number is added (unless asked not to). It is the one place the number is kept, because it is how "never call me again" is honoured |
| The audit trail | Records that an erasure was made and by whom, not the number |

Not removed by the engine, and to be done by hand:

- **Recordings.** They are held by the phone provider. The erasure returns their
  addresses once; delete them there.
- **Backups.** A backup taken before the erasure still holds the data until it
  ages out (`BACKUP_KEEP_DAYS`).
- **Logs.** Phone numbers and email addresses are masked in logs; a name spoken on
  a call is never logged.
- **What was already sent out**: a booking reported to your own system, a text
  message.
- **The event log** holds identifiers and outcomes, not names or numbers.

**Retention.** `retention_days` in the organization's settings removes call content
(the same fields as above) that many days after a call ended, hourly. Lead details
are not removed by retention, only by erasure.

## 4. What somebody qualified must decide, per market

1. Is consent needed for this kind of call (to consumers, to businesses, with an
   automated or AI voice)? In what form, and what must the record show?
2. Which public do-not-call registers apply, how are they obtained, and how often
   must they be re-read? The engine imports a list
   (`POST /api/do-not-call/import`); it does not fetch any register itself and does
   not know when a copy has gone stale.
3. What must the caller say at the start (who is calling, on whose behalf, that it
   is an AI, that the call is recorded), and may the call be recorded at all
   without consent from both sides?
4. Which hours and days are permitted? The engine uses the lead's time zone when
   the sheet gives one, and the deployment's default otherwise. It does not derive
   a time zone from a phone number.
5. How many attempts, how far apart, and how soon must an opt-out take effect?
6. How long may call content be kept, and on what legal basis is it processed?
   Is a data processing agreement needed with the providers of telephony, speech
   and AI, and where do they process the data?
7. Does the caller number have to be registered or verified with carriers so that
   calls are not blocked or labelled as spam?

## 5. Not built

| Item | Note |
| --- | --- |
| Fetching public registers automatically | Lists are imported by hand or by API |
| A time zone from the phone number | Only from the sheet |
| Rules per country or state (hours, attempts) chosen automatically from the number | One set of rules per campaign |
| Deleting recordings at the provider | Needs the provider; addresses are returned |
| Erasure by anything other than phone number (by email, by name) | Not built |
| Withdrawal of consent as its own event with a date | An opt-out puts the number on the do-not-call list; the consent record is cleared only by erasure |
| Exporting everything held about one person | Not built |
| Carrier registration of caller numbers | Done at the phone provider |
