"""Persist model context needed to resume approved turns."""

from alembic import op
import sqlalchemy as sa

revision = "0003_approval_resume_context"
down_revision = "0002_tool_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("tool_call_id", sa.String(length=100), nullable=True))
    op.add_column("approvals", sa.Column("messages", sa.JSON(), nullable=True))
    op.add_column("approvals", sa.Column("model", sa.String(length=200), nullable=True))
    op.add_column("approvals", sa.Column("provider", sa.String(length=100), nullable=True))
    op.add_column("approvals", sa.Column("conversation_id", sa.String(length=36), nullable=True))
    # Old pending approvals have no safe model continuation context.
    op.execute("UPDATE approvals SET status = 'expired' WHERE status = 'pending'")


def downgrade() -> None:
    op.drop_column("approvals", "conversation_id")
    op.drop_column("approvals", "provider")
    op.drop_column("approvals", "model")
    op.drop_column("approvals", "messages")
    op.drop_column("approvals", "tool_call_id")
