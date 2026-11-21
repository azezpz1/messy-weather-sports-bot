"""Store recurring collection schedules and execution history."""

import sqlalchemy as sa
from alembic import op

revision = "0002_collection_scheduler"
down_revision = "0001_forecast_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_schedules",
        sa.Column("sport", sa.Text, primary_key=True),
        sa.Column("interval_seconds", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Integer, nullable=False),
        sa.Column("next_due", sa.Text, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("interval_seconds > 0"),
        sa.CheckConstraint("enabled IN (0, 1)"),
    )
    op.create_table(
        "collection_executions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sport", sa.Text, sa.ForeignKey("collection_schedules.sport"), nullable=False),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("scheduled_for", sa.Text),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("finished_at", sa.Text),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("failure_summary", sa.Text),
    )
    op.create_index("collection_executions_by_sport", "collection_executions", ["sport", "id"])
    # SQLite can add a nullable inline reference while preserving the existing rows.
    op.execute(
        "ALTER TABLE collection_runs ADD COLUMN execution_id INTEGER "
        "REFERENCES collection_executions(id)"
    )
    op.create_index("collection_runs_by_execution", "collection_runs", ["execution_id"])


def downgrade() -> None:
    op.drop_index("collection_runs_by_execution", table_name="collection_runs")
    op.execute("ALTER TABLE collection_runs DROP COLUMN execution_id")
    op.drop_table("collection_executions")
    op.drop_table("collection_schedules")
