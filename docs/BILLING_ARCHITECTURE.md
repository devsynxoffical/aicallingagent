# Billing and usage

How the engine records what was used and what it comes to. This describes the code
as it is. Ids such as D-1 refer to `ENTERPRISE_BACKEND_AUDIT.md`.

**What this is and is not.** The engine keeps a usage ledger: measured usage, priced
at rates you configure and at list prices, corrected where a provider's own figure
is known. It enforces campaign budgets from that ledger. It does **not** produce
invoices, take payments, apply plans or taxes, or hold balances. No amount in it
has been compared with a provider's invoice: there are no provider credentials in
the environment this was built in, and every test uses stand-ins for the providers.
Treat the amounts as estimates until you have reconciled a month of real usage.

## 1. The ledger

Table `usage_records`. One row is one line.

| Column | Meaning |
| --- | --- |
| `organization_id`, `campaign_id`, `call_run_id` | Who and what the line is for. The call and the campaign are empty when the line belongs to neither |
| `kind` | `call` (everything billed by time: telephony, speech) or `ai_model` |
| `key` | What the line is for, unique. Writing the same fact twice finds the existing line |
| `quantity`, `unit` | Started minutes, or tokens |
| `quantity_seconds` | Call lines: the seconds the amount is based on |
| `amount_usd`, `currency` | Exact decimal (4 places); negative on a downward correction. USD only |
| `basis` | `estimate`: measured usage at a configured or list price. `provider`: the provider's own figure. `manual`: a person's correction |
| `adjusts_id` | On a correction: the line it corrects |
| `detail` | How the amount was arrived at: seconds, minutes, the rates applied, the source; or the workflow, model and token counts |
| `note`, `created_by` | On a correction: why, and who |

Rules, each checked by a test:

1. **A line is never changed or removed.** No code updates or deletes a ledger
   line. The database does not prevent it; a database administrator still can.
2. **A correction is a new line** pointing at the one it corrects, holding the
   difference. The history of every figure can be read back.
3. **Writing is idempotent.** The key is unique in the database. A provider report
   delivered twice, a job that ran twice, two processes writing at once: one line.
4. **Every figure is a sum of lines.** A campaign's spend, a call's displayed cost
   (`call_runs.cost_usd`), and the budget check all read the ledger. `cost_usd` is
   written only by the ledger code.

## 2. What writes a line

| When | Line | Amount |
| --- | --- | --- |
| A call ends | `call`, key `call:{id}:call` | Attempt fee + started minutes × per-minute rate. From the carrier's reported duration when that is what ended the call; from the engine's own clock when the engine ended it (the audio stopped, or the call was recovered after a restart) |
| The carrier's duration arrives after the engine's line, and differs | correction, key `…:carrier-duration` | The difference. The first line stays as written |
| The carrier reports the same call again, even with other figures | nothing | The first carrier report stands |
| An AI request or conversation is recorded with a known price | `ai_model`, key `ai:{usage id}` | The provider's token counts at the price table (`AI_ORCHESTRATION.md`, section 5) |
| The provider's price for a call is fetched (optional) | correction, key `…:provider-price`, basis `provider` | Brings the call's time-based total to the provider's figure |
| An operator corrects a line | correction, basis `manual` | What they enter, with a required note |

The engine's line and the status change that ends the call are written in the same
transaction. A call is therefore always either in progress (and counted against the
budget at its worst case) or ended with a line (and counted at what it cost), never
neither.

Not written:

- AI usage with no known price for its model. The usage row exists with an empty
  cost, and the usage report says its total is incomplete.
- AI usage that belongs to no campaign (the command-line rehearsal).
- Speech recognition and voice as separate lines. They are billed by time by most
  providers and are part of the per-minute rate you configure.
- Text messages. Not metered.

## 3. Rates

| Setting | Covers |
| --- | --- |
| `COST_PER_CALL_ATTEMPT_USD` | A fee per dial attempt |
| `COST_PER_CONNECTED_MINUTE_USD` | Everything billed by time: telephony, speech recognition, voice. By started minute. Can be set per campaign |
| `AI_PRICES`, built-in list prices | The AI model, per million tokens |
| `RECONCILE_PROVIDER_PRICE` | Off by default. When on, ten minutes after a call ends the phone provider is asked what it charged, and the call line is corrected to that figure. Retried while the provider has not priced the call yet. A price in another currency is logged and not applied |

**A change from earlier versions.** The per-minute rate used to be described as
covering the AI model too. The model is now measured and added separately, so a
rate that included it would count it twice. Take the model out of the rate.

**Before turning on provider prices**: the provider's price is telephony only. If
your per-minute rate also covers speech services, the correction would remove that
part. Use it only with a telephony-only rate.

Both rates default to zero, which means "not measured": lines are written with
amount 0 and budgets cannot work. The console's Setup screen and the rules endpoint
say so when a campaign has a budget and no rate.

A line records the rates it was written with. Changing a rate changes future lines
only.

## 4. Budgets

A campaign's `budget_usd` (calling rules) is enforced where a call is created
(D-3), not only in the dial loop:

- In the transaction that reserves the lead, with the campaign row locked, the
  engine adds what the ledger says was spent, the worst case of every outbound call
  still in progress, and the worst case of the call about to be placed. If that
  exceeds the budget the call is not created and the lead is left as it was.
- Worst case is a call lasting the campaign's time limit, at the configured rates.
- This applies to the dial loop, to an operator's manual dial, and to any other
  process claiming a call. Tested with eight simultaneous claims, half of them
  manual, against a budget that covers one call: one call is created (on SQLite
  and on PostgreSQL).
- The dial loop also checks the budget before picking a lead, so a campaign stops
  with the reason shown instead of failing claim after claim.

Limits to know:

- **The AI model's cost is not reserved in advance.** It is known when a call has
  been reviewed. Spend includes it from then on, so a budget can be passed by the
  AI cost of the calls in progress when the limit is reached.
- **Inbound calls are not refused for budget.** Someone is calling; they are
  answered, and their cost counts as spend.
- **Budgets are per campaign.** There is no balance or spending limit per
  organization.
- **With no rate configured**, worst case is zero and nothing is held back.

## 5. Reading it

| Endpoint | Who | Returns |
| --- | --- | --- |
| `GET /api/usage?days=30&campaign=name` | Any signed-in member of the organization | Total, totals by kind and basis, the newest lines (paged with `before_id`) |
| `POST /api/usage/{line_id}/adjustments` | The deployment's operator, acting in the line's organization | Adds a correction: `{"amount_usd": "-0.12", "note": "why"}` |
| `GET /api/usage/statements` | Any signed-in member | The months there is usage in, newest first, each with its total |
| `GET /api/usage/statements/2026-11` | Any signed-in member | That month's statement; `?format=csv` gives every line of the month |
| `GET /api/ai-usage` | Any signed-in member | AI use by workflow and model, including what could not be priced |
| `GET /api/campaigns/{name}/rules` | Any signed-in member | The budget and `spend_usd` (the ledger sum) |

An organization sees its own lines only (the same isolation as every table, with
row-level security on PostgreSQL). An organization's admin cannot correct a line:
that is for whoever bills them. Corrections appear in the audit trail.

Each line also produces a `UsageRecorded` event when it is a call's first line.

### Monthly statements

A statement is one calendar month (UTC) of an organization's ledger, added up:
calls (how many, billed minutes, amount), AI (requests, tokens, amount),
corrections, totals by basis and by campaign, and the grand total. Its number is
`<organization>-<year>-<month>`.

- **A line belongs to the month it was written in**, corrections included. A credit
  given in March for a call of February is on March's statement.
- So **the statement of a month that is over never changes**: no line is ever
  changed or backdated. Tested: writing a new call and a correction to an old line
  leaves the old month's statement identical.
- The current month is marked `open`.
- It is a statement of measured usage, and says so in its own text. It is **not a
  tax invoice**: no tax, no seller or buyer details, no payment. Turning it into an
  invoice needs those, and a payment provider.
- The CSV has one row per line, with cells a spreadsheet would run as formulas
  neutralised.

## 6. Moving an older database

Migration 0012 gives every existing line a key (`call:{call}:call`), its started
minutes, and the note that it predates the ledger. Amounts are not recomputed.
Rolling back is refused while the ledger holds corrections or AI lines, which the
previous one-line-per-call ledger cannot represent; it says how many and stops.

Spend used to be the sum of `call_runs.cost_usd`. It is now the sum of the ledger.
For calls that ended through the carrier's report the two are equal. A call that
only ever had the engine's estimate in `cost_usd` (no carrier report reached the
engine) had no ledger line before and still has none: its old estimate remains on
the call, and is no longer counted as spend.

## 7. Verified, and not

Verified by tests on SQLite and PostgreSQL:

- one line per finished call, with its breakdown; any number of repeated final
  reports add nothing;
- engine line then carrier report: a correction, the first line byte-for-byte as it
  was; carrier report then engine ending: one line;
- AI lines per usage record, once; nothing for an unknown price or no campaign;
- corrections: new line, original untouched, note required, no correction of a
  correction, not across organizations, operator only;
- the provider's price applied once, retried while missing, refused in another
  currency (the provider is a stand-in);
- concurrent claims against a budget.

Not verified:

- any amount against a real invoice;
- that the phone provider reports prices the way the code expects;
- the token counts of real conversations (they come from the voice library's usage
  reports, which have only been fed synthetic values here).

## 8. Open

| Item | State |
| --- | --- |
| Invoices, payments, plans, taxes, credits, balances | Not built. The ledger is what they would be drawn from |
| A spending limit per organization | Not built |
| Speech services and text messages as their own lines | Not built |
| Currencies other than USD | Not built |
| Reserving AI cost in the budget | Not built |
| Database-level protection of ledger lines (no UPDATE or DELETE for the application's role) | Not built; a permission change on PostgreSQL would do it |
| Monthly statements and exports | Built: statements per calendar month and a CSV of their lines. No PDF, no invoice numbering scheme an accountant would accept as is |
