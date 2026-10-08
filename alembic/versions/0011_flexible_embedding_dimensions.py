"""Allow embeddings from models with different vector dimensions."""

from alembic import op
import sqlalchemy as sa

revision = "0011_flex_embed_dims"
down_revision = "0010_conversation_project_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        op.alter_column("memory_entries", "embedding", existing_type=Vector(1536),
                        type_=Vector(), postgresql_using="embedding::vector")


def downgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        connection = op.get_bind()
        incompatible = connection.scalar(sa.text(
            "SELECT count(*) FROM memory_entries WHERE embedding IS NOT NULL AND vector_dims(embedding) <> 1536"
        ))
        if incompatible:
            raise RuntimeError("Cannot downgrade: memory contains embeddings that are not 1536-dimensional")
        op.alter_column("memory_entries", "embedding", existing_type=Vector(),
                        type_=Vector(1536), postgresql_using="embedding::vector(1536)")
