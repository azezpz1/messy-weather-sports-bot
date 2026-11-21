"""Store dashboard requests for independent collection."""

import sqlalchemy as sa
from alembic import op

revision = "0003_dashboard_refresh_requests"
down_revision = "0002_collection_scheduler"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_refresh_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sport", sa.Text, sa.ForeignKey("collection_schedules.sport"), nullable=False),
        sa.Column("requested_at", sa.Text, nullable=False),
        sa.Column(
            "execution_id", sa.Integer, sa.ForeignKey("collection_executions.id"), unique=True
        ),
    )
    op.create_index(
        "collection_refresh_requests_by_sport", "collection_refresh_requests", ["sport", "id"]
    )
    op.create_index(
        "collection_refresh_requests_pending",
        "collection_refresh_requests",
        ["requested_at", "id"],
        sqlite_where=sa.text("execution_id IS NULL"),
    )

    op.create_index(
        "collection_runs_by_sport_day",
        "collection_runs",
        ["sport", "game_day"],
        sqlite_where=sa.text("execution_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("collection_runs_by_sport_day", table_name="collection_runs")
    op.drop_table("collection_refresh_requests")
