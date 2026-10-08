"""Reserve estimated cost before starting provider requests."""

from alembic import op
import sqlalchemy as sa

revision = "0009_usage_reservations"
down_revision = "0008_monthly_usage_costs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_reservations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_reservations_task_id", "usage_reservations", ["task_id"])
    op.create_index("ix_usage_reservations_session_id", "usage_reservations", ["session_id"])
    op.create_index("ix_usage_reservations_reserved_at", "usage_reservations", ["reserved_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_reservations_reserved_at", table_name="usage_reservations")
    op.drop_index("ix_usage_reservations_session_id", table_name="usage_reservations")
    op.drop_index("ix_usage_reservations_task_id", table_name="usage_reservations")
    op.drop_table("usage_reservations")
