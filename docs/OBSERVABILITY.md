# Observability

What the engine tells you about itself, and how to use it. This describes the code
as it is. Ids such as E-1 refer to `ENTERPRISE_BACKEND_AUDIT.md`.

**What has and has not been checked.** The logs, health checks, metrics and audit
trail are covered by automated tests. No monitoring system has been connected: the
alert rules have been checked to be valid YAML and to refer only to metrics that
exist, not loaded into Prometheus or seen to fire. No dashboard is shipped.

## 1. Logs

One JSON object per line on standard output (`LOG_FORMAT=json`, the default), or
readable lines for a terminal (`LOG_FORMAT=text`).

```json
{"time": "2026-11-14T19:02:11.412+00:00", "level": "warning", "logger": "callagent.dialer.reconcile",
 "message": "call 812: the provider could not be asked (TimeoutError); left unchanged",
 "request_id": "9f1c…", "call_id": 812, "job_id": 4411, "organization_id": 3}
```

| Field | Present when | Use |
| --- | --- | --- |
| `request_id` | Inside an HTTP request, and in every job that request caused | Follow one action from the console through the engine |
| `call_id` | Inside a call's audio pipeline and the jobs about that call | Everything about one call |
| `job_id` | Inside a background job | One attempt of one job |
| `organization_id` | Inside a signed-in request | Whose data the line is about |

- A request's id is taken from `X-Request-ID` when the caller sends a well-formed
  one (8 to 64 letters, digits, `.`, `_`, `-`), otherwise issued. It is returned in
  the response's `X-Request-ID` header and stored on every job the request enqueues
  (`jobs.request_id`), so a failure hours later can be traced to the click that
  caused it.
- Everything about one call is also in the event log: `GET /api/events?call_run_id=…`.

### Personal data

- Phone numbers and email addresses are masked in every line, including exception
  text and URL-encoded numbers in request paths: `+14155550123` becomes
  `+14*******23`, `jane@firm.com` becomes `j***@firm.com`.
- The voice library is held at `VOICE_LOG_LEVEL=WARNING`. Below that it prints the
  whole of a call's instructions and every sentence spoken.
- The HTTP, AI and phone provider client libraries are held at warning, so request
  addresses are not logged line by line.
- The engine's own code does not log transcripts, prompts or request bodies.

Masking recognises numbers in international form (a `+` and 8 to 15 digits). A
number written another way inside free text (a transcript line, a note) would not
be recognised, which is why such text is not logged at all.

## 2. Health

| Endpoint | Says | Use for |
| --- | --- | --- |
| `GET /health/live` | The process answers | Restarting a hung process (liveness probe) |
| `GET /health/ready` | The database answers, its schema is the one this release needs, the background worker runs. 503 and the failing check otherwise | Sending traffic (readiness probe); deploy checks |
| `GET /health` | `{"ok": true}` | Older callers. It no longer lists missing settings; those are at `/api/status` for signed-in callers |

All three are public and say nothing about configuration. Readiness answers within
5 seconds even when the database does not.

Readiness does not check the phone, speech or AI providers: a provider outage must
not take the engine out of rotation while calls are in progress. Whether providers
are configured is on the console's Setup screen (`/api/status`); whether they work
shows in the call metrics below.

## 3. Metrics

`GET /metrics`, Prometheus text format, with `Authorization: Bearer <METRICS_TOKEN>`
(or the engine's API token). The figures cover every organization of the deployment
and are for its operator; an organization's admin cannot read them. No label holds
a phone number, a name or a campaign.

Counted by the process as things happen (reset when it restarts):

| Metric | Labels | What |
| --- | --- | --- |
| `callagent_http_requests_total` | method, route, status | Requests, by the route as declared (`/api/campaigns/{name}`), unknown paths as `unmatched` |
| `callagent_http_request_duration_seconds` | method, route | Histogram |
| `callagent_calls_finished_total` | direction, outcome | Calls that ended, by disposition |
| `callagent_call_response_seconds` | | Per call: median time from the caller finishing to the agent's first audio |
| `callagent_call_model_failures_total` | | Turns of live calls the AI model failed to answer |
| `callagent_campaigns_stopped_total` | reason | Campaigns the engine stopped itself: `provider_failure`, `budget`, `opt_outs`, `target`, `ended`, `finished`, `other` |
| `callagent_jobs_finished_total` | kind, queue, outcome | Job attempts: `ok`, `retry`, `timeout`, `dead` |
| `callagent_job_duration_seconds` | kind | Histogram |
| `callagent_ai_requests_total` | workflow, outcome | AI requests; a call's turns count under `live_call` |
| `callagent_ai_tokens_total` | workflow, kind | Tokens as the provider reported them |
| `callagent_ai_request_duration_seconds` | workflow | Histogram, retries included |
| `callagent_ai_cost_usd_total` | workflow | At the price table in force |
| `callagent_ai_usage_unrecorded_total`, `callagent_audit_unrecorded_total` | | Records that could not be written |

Read from the database and the process at each scrape (correct after a restart):

| Metric | Labels | What |
| --- | --- | --- |
| `callagent_calls_active` | status | Calls not yet ended |
| `callagent_campaigns_running` | | Campaigns dialing |
| `callagent_jobs` | queue, status | Jobs queued, running and dead |
| `callagent_jobs_oldest_due_seconds` | queue | How long the longest-waiting due job has waited |
| `callagent_db_pool_in_use`, `callagent_db_pool_size` | | Database connections (PostgreSQL) |
| `callagent_thread_pool_backlog` | pool | Work waiting for a thread: `db` is the request and call path, `background` the slow work |
| `callagent_worker_running`, `callagent_ready` | | 1 or 0 |

A scrape runs four small queries. Scrape every 15 to 30 seconds.

With more than one engine process, the counters are per process and Prometheus sums
them; the gauges read from the database would be reported by each process and must
be taken from one (`max`), not summed. One process is the only supported setup
today (D-6).

Not exported: speech-to-text and voice latency as metrics. They are measured per
call and stored on the call (`summary.latency`), and reported per campaign in the
console; only the end-to-end response time is a metric.

## 4. Alerts

`ops/prometheus-alerts.yml` holds 16 rules, each with a summary and what to do:

| Area | Rules |
| --- | --- |
| Availability | Engine down; background worker stopped; server-error rate above 2%; slow requests |
| Calls | A campaign stopped because the provider is failing; the model failing during calls; slow answers on calls |
| Background work | Call-related work waiting; analysis backlog; jobs that gave up; AI provider unavailable |
| Capacity | Database pool nearly full; call-path threads backed up |
| Records | Usage not recorded; audit not recorded; a scheduled backup failed |

The thresholds are starting points, set by reasoning and not from traffic. Review
them after the first weeks of real calls.

## 5. Audit trail

Table `audit_log`: who tried to change what, when, from where and how it ended.

- **What is recorded**: every `POST`, `PUT`, `PATCH` and `DELETE` on `/api/…` by an
  authenticated caller, including attempts that were refused (403) or failed; and
  every sign-in attempt on a known account (`sign_in`, `sign_in_failed`,
  `sign_in_failed_account_locked`, `sign_in_refused_locked`,
  `sign_in_refused_deactivated`).
- **Fields**: organization, actor (the person's email, or `api token`), their role,
  the operation's name (`start_campaign`, `set_rules`, `approve_version`, …), the
  method and declared route, the route's own parameters (which campaign, which
  user), the HTTP status, the request id, the address the request came from.
- **What is not recorded**: the request body. It can hold passwords and customer
  data. The audit says that the rules of campaign "q4" were changed and by whom;
  what they were changed to is in the campaign's own history where one exists
  (playbook versions) and otherwise not kept.
- **Reads** are not recorded.
- **Append-only**: no code updates or deletes these rows (a test checks the source).
  The database does not enforce that; a database administrator can still change
  them. Shipping the audit to write-once storage is not built.
- **Who can read it**: `GET /api/audit` for the organization's admins, their own
  organization only (the same isolation as every other table, including row-level
  security on PostgreSQL). Filters: `actor`, `action`; paging with `before_id`.

Two limits to know:

1. An entry for an API change is written right after the request, in its own
   transaction. If the process dies between the change and the entry, the change
   exists without its entry. Sign-in entries are written in the same transaction as
   the sign-in.
2. The address is taken from `X-Forwarded-For` when present. That is only
   trustworthy behind a proxy that sets it; run the engine behind one.

There is no console screen for the audit trail yet; it is an API.

## 6. Finding out what happened

| Question | Where |
| --- | --- |
| What happened on this call? | `GET /api/calls/{id}` (status, transcript, tools, latency) and `GET /api/events?call_run_id={id}`; logs with `call_id` |
| Why did this campaign stop? | The campaign's stop reason in the console; `CampaignStopped` events |
| Why is this document or training run stuck? | `GET /api/jobs` (queued, running, dead with the last error) |
| Who changed this? | `GET /api/audit` |
| What did the AI cost, and for what? | `GET /api/ai-usage` |
| Is it healthy now? | `/health/ready`, `/metrics` |

## 7. Not built

| Item | Note |
| --- | --- |
| Error reporting to an external service | Errors are in the logs with tracebacks (masked) and counted in metrics |
| Distributed tracing | One process; the ids above cover it. Revisit when a second service exists |
| Dashboards | Not shipped |
| Log shipping and retention | The engine writes to standard output; collecting and keeping logs is the platform's job |
| An audit screen in the console | API only |
| Speech provider latency as metrics | Stored per call, not exported |
