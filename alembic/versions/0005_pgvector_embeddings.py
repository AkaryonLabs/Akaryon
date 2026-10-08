"""Use pgvector for embeddings on PostgreSQL deployments."""

from alembic import op
import sqlalchemy as sa

revision = "0005_pgvector_embeddings"
down_revision = "0004_memory_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.alter_column("memory_entries", "embedding", existing_type=sa.JSON(),
                        type_=Vector(1536), postgresql_using="embedding::text::vector")


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        op.alter_column("memory_entries", "embedding", existing_type=Vector(1536),
                        type_=sa.JSON(), postgresql_using="embedding::text::json")
