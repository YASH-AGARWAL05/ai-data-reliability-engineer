"""Run migrations against the configured PostgreSQL database."""

from alembic import context
from sqlalchemy import create_engine

from ai_data_reliability.service.config import Settings
from ai_data_reliability.service.db import Base

config = context.config


def run_migrations_online() -> None:
    engine = create_engine(Settings.from_env().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
