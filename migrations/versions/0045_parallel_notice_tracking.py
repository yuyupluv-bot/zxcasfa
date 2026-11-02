"""Track one current aggregate parallel-order notice per driver.

Revision ID: 0045_parallel_notice_tracking
Revises: 0044_scheduled_temporary_driver
"""
from alembic import op
import sqlalchemy as sa

revision = "0045_parallel_notice_tracking"
down_revision = "0044_scheduled_temporary_driver"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "parallel_notice_outbox_id" not in columns:
        op.add_column("users", sa.Column("parallel_notice_outbox_id", sa.Integer(), nullable=True))
    if "parallel_notice_count" not in columns:
        op.add_column(
            "users",
            sa.Column(
                "parallel_notice_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "parallel_notice_count" in columns:
        op.drop_column("users", "parallel_notice_count")
    if "parallel_notice_outbox_id" in columns:
        op.drop_column("users", "parallel_notice_outbox_id")
