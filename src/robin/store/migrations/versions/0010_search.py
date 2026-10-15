"""Full-text search (FR-040, TR-081, TR-082): an FTS5 index with secrets masked at index time.

Revision ID: 0010_search
Revises: 0009_grants
Create Date: 2026-10-15
"""

from alembic import op

revision = "0010_search"
down_revision = "0009_grants"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE VIRTUAL TABLE search_index USING fts5(
        title,
        body,
        kind UNINDEXED,
        ref UNINDEXED,
        session_id UNINDEXED,
        task_id UNINDEXED,
        project_id UNINDEXED,
        runtime UNINDEXED,
        at UNINDEXED,
        tokenize = 'unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TABLE search_doc (
        doc_key VARCHAR NOT NULL,
        fts_rowid INTEGER NOT NULL,
        indexed_at VARCHAR(27) NOT NULL,
        CONSTRAINT pk_search_doc PRIMARY KEY (doc_key),
        CONSTRAINT uq_search_doc_fts_rowid UNIQUE (fts_rowid)
    )
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Robin doesn't downgrade schemas; restore a pre-migration backup (TR-073)")
