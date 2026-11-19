"""Run Alembic revisions on the locked SQLite forecast database."""

from alembic import context
from alembic.util import CommandError
from sqlalchemy import Connection

from messy_weather_sports_bot.collection import forecast_db_transaction


def run_migrations(db: Connection) -> None:
    context.configure(connection=db, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise CommandError("Forecast history migrations require a SQLite connection")

connection = context.config.attributes.get("connection")
if connection is None:
    with forecast_db_transaction() as db:
        run_migrations(db)
else:
    run_migrations(connection)
