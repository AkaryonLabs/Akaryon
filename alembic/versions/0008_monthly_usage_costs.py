"""Persist estimated provider usage for monthly budget enforcement."""

from alembic import op
import sqlalchemy as sa

revision = "0008_monthly_usage_costs"
down_revision = "0007_approval_proposal_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_costs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_costs_task_id", "usage_costs", ["task_id"])
    op.create_index("ix_usage_costs_session_id", "usage_costs", ["session_id"])
    op.create_index("ix_usage_costs_occurred_at", "usage_costs", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_costs_occurred_at", table_name="usage_costs")
    op.drop_index("ix_usage_costs_session_id", table_name="usage_costs")
    op.drop_index("ix_usage_costs_task_id", table_name="usage_costs")
    op.drop_table("usage_costs")
