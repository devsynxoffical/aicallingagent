"""Baseline: the schema as it stood when migrations were introduced.

Databases older than this are brought to this shape by ``migrate.upgrade`` (additive
columns only) and then recorded here. There is nothing to run.
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
