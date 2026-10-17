from alembic import context
from sqlalchemy import engine_from_config, pool

engine = engine_from_config(context.config.get_section(context.config.config_ini_section) or {}, prefix="sqlalchemy.", poolclass=pool.NullPool)
with engine.connect() as connection:
    connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.commit()
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
