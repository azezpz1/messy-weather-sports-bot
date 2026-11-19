"""Create the SQLite forecast history schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001_forecast_history"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    # Adopt databases created by the original PR before it used Alembic.
    (legacy_version,) = db.exec_driver_sql("PRAGMA user_version").one()
    if legacy_version == 1 and {
        "collection_runs",
        "games",
        "game_observations",
        "forecast_periods",
    }.issubset(sa.inspect(db).get_table_names()):
        return

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sport", sa.Text, nullable=False),
        sa.Column("game_day", sa.Text, nullable=False),
        sa.Column("started_at", sa.Text, nullable=False),
        sa.Column("collected_at", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("games_found", sa.Integer),
        sa.Column("rankings_unavailable", sa.Integer, nullable=False),
        sa.Column("error_type", sa.Text),
    )
    op.create_table(
        "games",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sport", sa.Text, nullable=False),
        sa.Column("source_key", sa.Text, nullable=False),
        sa.Column("espn_event_id", sa.Text),
        sa.UniqueConstraint("sport", "source_key"),
    )
    op.create_table(
        "game_observations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run_id", sa.Integer, sa.ForeignKey("collection_runs.id"), nullable=False),
        sa.Column("game_id", sa.Integer, sa.ForeignKey("games.id"), nullable=False),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.Column("home_team", sa.Text, nullable=False),
        sa.Column("away_team", sa.Text, nullable=False),
        sa.Column("kickoff", sa.Text, nullable=False),
        sa.Column("espn_venue_id", sa.Text),
        sa.Column("espn_venue_name", sa.Text),
        sa.Column("stadium_name", sa.Text),
        sa.Column("stadium_venue_id", sa.Text),
        sa.Column("latitude", sa.REAL),
        sa.Column("longitude", sa.REAL),
        sa.Column("is_covered", sa.Integer),
        sa.Column("neutral_site", sa.Integer, nullable=False),
        sa.Column("home_rank", sa.Integer),
        sa.Column("away_rank", sa.Integer),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("alerts_error_type", sa.Text),
        sa.Column("is_messy", sa.Integer),
        sa.Column("condition", sa.Text),
        sa.Column("score", sa.REAL),
        sa.Column("alert_event", sa.Text),
        sa.Column("alert_severity", sa.Text),
        sa.Column("alert_onset", sa.Text),
        sa.Column("alert_ends", sa.Text),
    )
    op.create_index("game_observations_by_game", "game_observations", ["game_id", "observed_at"])
    op.create_index("game_observations_by_run", "game_observations", ["run_id"])
    op.create_table(
        "forecast_periods",
        sa.Column(
            "observation_id", sa.Integer, sa.ForeignKey("game_observations.id"), primary_key=True
        ),
        sa.Column("period_index", sa.Integer, primary_key=True),
        sa.Column("starts_at", sa.Text),
        sa.Column("ends_at", sa.Text),
        sa.Column("short_forecast", sa.Text, nullable=False),
        sa.Column("temperature_f", sa.Integer),
        sa.Column("wind_speed_mph", sa.REAL, nullable=False),
        sa.Column("precipitation_probability", sa.Integer),
        sa.Column("relative_humidity", sa.Integer),
        sa.Column("feels_like_f", sa.REAL),
        sa.Column("selected", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("forecast_periods")
    op.drop_table("game_observations")
    op.drop_table("games")
    op.drop_table("collection_runs")
