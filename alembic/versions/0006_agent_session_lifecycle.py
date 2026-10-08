"""Add task-linked agent session lifecycle details."""

from alembic import op
import sqlalchemy as sa

revision = "0006_agent_session_lifecycle"
down_revision = "0005_pgvector_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_sessions", sa.Column("task_id", sa.String(length=36), nullable=True))
    op.add_column("agent_sessions", sa.Column("conversation_id", sa.String(length=36), nullable=True))
    op.add_column("agent_sessions", sa.Column("status", sa.String(length=32), nullable=False,
                                               server_default="pending"))
    op.add_column("agent_sessions", sa.Column("model", sa.String(length=200), nullable=True))
    op.add_column("agent_sessions", sa.Column("provider", sa.String(length=100), nullable=True))
    op.add_column("agent_sessions", sa.Column("result", sa.Text(), nullable=True))
    op.add_column("agent_sessions", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("agent_sessions", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE agent_sessions SET updated_at = created_at WHERE updated_at IS NULL")
    op.create_index("ix_agent_sessions_task_id", "agent_sessions", ["task_id"])
    op.create_index("ix_agent_sessions_conversation_id", "agent_sessions", ["conversation_id"])
    op.create_index("ix_agent_sessions_status", "agent_sessions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_status", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_conversation_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_task_id", table_name="agent_sessions")
    op.drop_column("agent_sessions", "updated_at")
    op.drop_column("agent_sessions", "error")
    op.drop_column("agent_sessions", "result")
    op.drop_column("agent_sessions", "provider")
    op.drop_column("agent_sessions", "model")
    op.drop_column("agent_sessions", "status")
    op.drop_column("agent_sessions", "conversation_id")
    op.drop_column("agent_sessions", "task_id")
