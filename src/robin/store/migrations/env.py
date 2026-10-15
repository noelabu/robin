"""Alembic environment. Robin runs migrations programmatically on a connection it owns (store/migrate.py)."""

from alembic import context

from robin.store.models import Base

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,
    transaction_per_migration=False,
)
context.run_migrations()
