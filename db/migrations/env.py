import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context

# Make the api/ root importable ("config", "db", ...) regardless of cwd.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import db.models  # noqa: E402,F401  (import registers models on Base.metadata)
from config import DATABASE_URL  # noqa: E402
from db.engine import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        from sqlalchemy import engine_from_config, make_url, pool
        from sqlalchemy.ext.asyncio import async_engine_from_config

        if make_url(DATABASE_URL).get_dialect().is_async:
            async def run_async_migrations() -> None:
                engine = async_engine_from_config(
                    {"sqlalchemy.url": DATABASE_URL},
                    prefix="sqlalchemy.",
                    poolclass=pool.NullPool,
                )
                async with engine.connect() as connection:
                    await connection.run_sync(do_run_migrations)
                await engine.dispose()

            asyncio.run(run_async_migrations())
            return

        connectable = engine_from_config(
            {"sqlalchemy.url": DATABASE_URL},
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
