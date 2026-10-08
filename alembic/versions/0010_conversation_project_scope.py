"""Bind conversations to a project so their history cannot cross project contexts."""

from alembic import op
import sqlalchemy as sa

revision = "0010_conversation_project_scope"
down_revision = "0009_usage_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_conversations_project_id_projects", "projects", ["project_id"], ["id"],
            ondelete="RESTRICT")
        batch_op.create_index("ix_conversations_project_id", ["project_id"])


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_index("ix_conversations_project_id")
        batch_op.drop_constraint("fk_conversations_project_id_projects", type_="foreignkey")
        batch_op.drop_column("project_id")
