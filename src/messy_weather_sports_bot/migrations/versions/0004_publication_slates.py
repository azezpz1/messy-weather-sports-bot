"""Record which independent collection runs covered the whole game day."""

import sqlalchemy as sa
from alembic import op

revision = "0004_publication_slates"
down_revision = "0003_dashboard_refresh_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection_runs", sa.Column("publication_game_count", sa.Integer))
    op.create_index(
        "independent_collection_runs_by_day",
        "collection_runs",
        ["sport", "game_day", "id"],
        sqlite_where=sa.text("execution_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("independent_collection_runs_by_day", table_name="collection_runs")
    op.drop_column("collection_runs", "publication_game_count")
