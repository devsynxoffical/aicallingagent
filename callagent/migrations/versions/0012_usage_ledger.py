"""The usage ledger: a key per line, how each amount was arrived at, and corrections as new lines."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("usage_records")}
    indexes = {i["name"] for i in inspector.get_indexes("usage_records")}
    if "key" in columns:
        return  # a database created by this release already has the ledger's shape

    if "uq_usage_records_call_kind" in indexes:
        op.drop_index("uq_usage_records_call_kind", table_name="usage_records")
    # One batch: on SQLite the table is rebuilt once; on PostgreSQL these are plain ALTERs.
    with op.batch_alter_table("usage_records") as batch:
        batch.add_column(sa.Column("key", sa.String(160), nullable=True))
        batch.add_column(sa.Column("quantity", sa.Numeric(18, 4), nullable=False, server_default="0"))
        batch.add_column(sa.Column("unit", sa.String(16), nullable=False, server_default="minute"))
        batch.add_column(sa.Column("currency", sa.String(3), nullable=False, server_default="USD"))
        batch.add_column(sa.Column("basis", sa.String(12), nullable=False, server_default="estimate"))
        batch.add_column(sa.Column("adjusts_id", sa.Integer, nullable=True))
        batch.add_column(sa.Column("detail", sa.JSON, nullable=True))
        batch.add_column(sa.Column("note", sa.String(300), nullable=False, server_default=""))
        batch.add_column(sa.Column("created_by", sa.String(254), nullable=False, server_default=""))
        batch.alter_column("campaign_id", existing_type=sa.Integer, nullable=True)
        batch.alter_column("call_run_id", existing_type=sa.Integer, nullable=True)

    # Existing lines were one per call and kind: that is their key. Their minutes are the started minutes of their seconds.
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("UPDATE usage_records SET key = 'call:' || call_run_id || ':' || kind, quantity = CEIL(quantity_seconds / 60.0), detail = '{\"source\": \"before the ledger\"}'::json"))
    else:
        bind.execute(sa.text("UPDATE usage_records SET key = 'call:' || call_run_id || ':' || kind, quantity = (quantity_seconds + 59) / 60, detail = '{\"source\": \"before the ledger\"}'"))

    with op.batch_alter_table("usage_records") as batch:
        batch.alter_column("key", existing_type=sa.String(160), nullable=False)
        batch.alter_column("detail", existing_type=sa.JSON, nullable=False)
        batch.create_foreign_key("fk_usage_records_adjusts", "usage_records", ["adjusts_id"], ["id"])
        batch.create_check_constraint("ck_usage_records_basis", "basis IN ('estimate', 'provider', 'manual')")
    op.create_index("uq_usage_records_key", "usage_records", ["key"], unique=True)
    if "ix_usage_records_call_run_id" not in indexes:
        op.create_index("ix_usage_records_call_run_id", "usage_records", ["call_run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    extra = bind.execute(sa.text("SELECT COUNT(*) FROM usage_records WHERE adjusts_id IS NOT NULL OR call_run_id IS NULL OR kind <> 'call'")).scalar()
    if extra:
        raise RuntimeError(
            f"{extra} ledger lines (corrections, AI usage) have no place in the previous release's ledger, which held one line per call. "
            "Rolling back would have to delete them. Export them first and remove them deliberately, then run the downgrade again."
        )
    op.drop_index("uq_usage_records_key", table_name="usage_records")
    op.drop_index("ix_usage_records_call_run_id", table_name="usage_records")
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_constraint("ck_usage_records_basis", type_="check")
        # adjusts_id takes its foreign key with it (which has no name in a database created by this release).
        for column in ("created_by", "note", "detail", "adjusts_id", "basis", "currency", "unit", "quantity", "key"):
            batch.drop_column(column)
        batch.alter_column("call_run_id", existing_type=sa.Integer, nullable=False)
        batch.alter_column("campaign_id", existing_type=sa.Integer, nullable=False)
    op.create_index("uq_usage_records_call_kind", "usage_records", ["call_run_id", "kind"], unique=True)
