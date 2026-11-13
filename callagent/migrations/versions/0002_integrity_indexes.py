"""Uniqueness the code used to enforce by looking first, now enforced by the database.

Before any index is created the data is checked. If rows already break a rule, the
migration stops, names them, and changes nothing: which duplicate to keep is a
decision about real leads and calls, not one a migration should make.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ACTIVE = "status IN ('created', 'queued', 'initiated', 'ringing', 'in-progress')"

# (index name, table, columns, unique, WHERE for a partial index)
INDEXES = [
    ("uq_campaigns_inbound_number", "campaigns", ["inbound_number"], True, None),
    ("uq_leads_campaign_phone", "leads", ["campaign_id", "phone_e164"], True, None),
    ("ix_leads_due", "leads", ["campaign_id", "status", "next_attempt_at"], False, None),
    ("uq_call_runs_twilio_call_sid", "call_runs", ["twilio_call_sid"], True, None),
    ("uq_call_runs_one_active_outbound_per_lead", "call_runs", ["lead_id"], True, f"direction = 'outbound' AND {ACTIVE}"),
    ("ix_call_runs_campaign_status", "call_runs", ["campaign_id", "status"], False, None),
    ("ix_call_runs_lead_latest", "call_runs", ["lead_id", "id"], False, None),
    ("uq_appointments_call_start", "appointments", ["call_run_id", "starts_at"], True, None),
    ("uq_playbook_versions_campaign_version", "playbook_versions", ["campaign_id", "version"], True, None),
    ("uq_playbook_versions_one_active", "playbook_versions", ["campaign_id"], True, "status = 'active'"),
    ("uq_experiments_one_running", "experiments", ["campaign_id"], True, "status = 'running'"),
]

# What each unique rule means, and the query that finds rows breaking it.
CHECKS = [
    ("the same provider call recorded more than once (call_runs.twilio_call_sid)",
     "SELECT twilio_call_sid, COUNT(*) FROM call_runs WHERE twilio_call_sid IS NOT NULL GROUP BY twilio_call_sid HAVING COUNT(*) > 1"),
    ("the same phone number more than once in a campaign (leads.campaign_id, phone_e164)",
     "SELECT campaign_id, phone_e164, COUNT(*) FROM leads GROUP BY campaign_id, phone_e164 HAVING COUNT(*) > 1"),
    ("more than one unfinished outbound call for a lead (call_runs.lead_id)",
     f"SELECT lead_id, COUNT(*) FROM call_runs WHERE direction = 'outbound' AND {ACTIVE} GROUP BY lead_id HAVING COUNT(*) > 1"),
    ("a number answered by more than one campaign (campaigns.inbound_number)",
     "SELECT inbound_number, COUNT(*) FROM campaigns WHERE inbound_number IS NOT NULL GROUP BY inbound_number HAVING COUNT(*) > 1"),
    ("the same meeting booked twice on one call (appointments.call_run_id, starts_at)",
     "SELECT call_run_id, starts_at, COUNT(*) FROM appointments WHERE call_run_id IS NOT NULL GROUP BY call_run_id, starts_at HAVING COUNT(*) > 1"),
    ("a playbook version number used twice in a campaign (playbook_versions.campaign_id, version)",
     "SELECT campaign_id, version, COUNT(*) FROM playbook_versions GROUP BY campaign_id, version HAVING COUNT(*) > 1"),
    ("more than one live playbook version in a campaign (playbook_versions.campaign_id)",
     "SELECT campaign_id, COUNT(*) FROM playbook_versions WHERE status = 'active' GROUP BY campaign_id HAVING COUNT(*) > 1"),
    ("more than one running A/B test in a campaign (experiments.campaign_id)",
     "SELECT campaign_id, COUNT(*) FROM experiments WHERE status = 'running' GROUP BY campaign_id HAVING COUNT(*) > 1"),
]


def find_problems(bind) -> list[str]:
    problems = []
    for meaning, query in CHECKS:
        rows = bind.execute(sa.text(query)).fetchmany(5)
        if rows:
            problems.append(f"{meaning}: " + "; ".join(str(tuple(r)) for r in rows))
    return problems


def upgrade() -> None:
    bind = op.get_bind()
    problems = find_problems(bind)
    if problems:
        raise RuntimeError(
            "This database has rows that break rules the new version enforces. Nothing was changed. "
            "Resolve them (`callagent db check` lists them again), then start again:\n- " + "\n- ".join(problems)
        )
    existing = {i["name"] for table in {t for _, t, *_ in INDEXES} for i in sa.inspect(bind).get_indexes(table)}
    for name, table, columns, unique, where in INDEXES:
        if name in existing:
            continue
        clause = sa.text(where) if where else None
        op.create_index(name, table, columns, unique=unique, sqlite_where=clause, postgresql_where=clause)


def downgrade() -> None:
    bind = op.get_bind()
    existing = {i["name"] for table in {t for _, t, *_ in INDEXES} for i in sa.inspect(bind).get_indexes(table)}
    for name, table, *_ in reversed(INDEXES):
        if name in existing:
            op.drop_index(name, table_name=table)
