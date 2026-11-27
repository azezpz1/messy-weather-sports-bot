"""Persist publishing schedules and their execution history."""

import sqlalchemy as sa
from alembic import op

revision = "0005_publication_schedules"
down_revision = "0004_publication_slates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "publication_schedules",
        sa.Column("sport", sa.Text, primary_key=True),
        sa.Column("run_time", sa.Text, nullable=False),
        sa.Column("weekdays", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Integer, nullable=False),
        sa.Column("next_due", sa.Text),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        sa.CheckConstraint("weekdays BETWEEN 1 AND 127"),
        sa.CheckConstraint(
            "(enabled = 0 AND next_due IS NULL) OR (enabled = 1 AND next_due IS NOT NULL)"
        ),
    )
    op.create_table(
        "publication_executions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sport", sa.Text, sa.ForeignKey("publication_schedules.sport"), nullable=False),
        sa.Column("game_day", sa.Text, nullable=False),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("requested_at", sa.Text),
        sa.Column("scheduled_for", sa.Text),
        sa.Column("started_at", sa.Text),
        sa.Column("finished_at", sa.Text),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("failure_summary", sa.Text),
    )
    op.create_index(
        "publication_executions_by_day", "publication_executions", ["sport", "game_day"]
    )
    op.create_index(
        "publication_scheduled_day",
        "publication_executions",
        ["sport", "game_day"],
        unique=True,
        sqlite_where=sa.text("scheduled_for IS NOT NULL"),
    )
    op.create_index(
        "publication_active_by_day",
        "publication_executions",
        ["sport", "game_day"],
        unique=True,
        sqlite_where=sa.text("outcome IN ('queued', 'running')"),
    )
    op.create_table(
        "publication_confirmations",
        sa.Column("sport", sa.Text, primary_key=True),
        sa.Column("request_key", sa.Text, primary_key=True),
        sa.Column(
            "execution_id", sa.Integer, sa.ForeignKey("publication_executions.id"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("publication_confirmations")
    op.drop_table("publication_executions")
    op.drop_table("publication_schedules")
