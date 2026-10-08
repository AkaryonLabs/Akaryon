"""Bind external agent approvals to a scoped, expiring proposal."""

from alembic import op
import sqlalchemy as sa

revision = "0007_approval_proposal_binding"
down_revision = "0006_agent_session_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("scope", sa.String(length=2048), nullable=True))
    op.add_column("approvals", sa.Column("proposal_digest", sa.String(length=64), nullable=True))
    op.add_column("approvals", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("approvals", "expires_at")
    op.drop_column("approvals", "proposal_digest")
    op.drop_column("approvals", "scope")
