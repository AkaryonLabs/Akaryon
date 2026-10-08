"""Persist revocable hosted sessions and single-use OAuth login challenges."""
from alembic import op
import sqlalchemy as sa

revision = "0012_hosted_signin"
down_revision = "0011_flex_embed_dims"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hosted_sessions",
                    sa.Column("token_hash", sa.String(64), primary_key=True),
                    sa.Column("github_id", sa.String(32), nullable=False),
                    sa.Column("email", sa.String(320), nullable=False),
                    sa.Column("expires_at", sa.Integer(), nullable=False))
    op.create_index("ix_hosted_sessions_expires_at", "hosted_sessions", ["expires_at"])
    op.create_table("hosted_logins",
                    sa.Column("state_hash", sa.String(64), primary_key=True),
                    sa.Column("verifier", sa.String(128), nullable=False),
                    sa.Column("expires_at", sa.Integer(), nullable=False))
    op.create_index("ix_hosted_logins_expires_at", "hosted_logins", ["expires_at"])


def downgrade():
    op.drop_table("hosted_logins")
    op.drop_table("hosted_sessions")
