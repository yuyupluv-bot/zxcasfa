"""Add optional driver shortage and passenger en-route tracking."""
from alembic import op
import sqlalchemy as sa

revision = "0056_enroute_notice"
down_revision = "0055_chat_notice_author"
branch_labels = None
depends_on = None


def upgrade():
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("orders")}
    if "enroute_notice_outbox_id" not in columns:
        op.add_column("orders", sa.Column("enroute_notice_outbox_id", sa.Integer(), nullable=True))
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("users")}
    if "shortage_notice_enabled" not in columns:
        op.add_column("users", sa.Column("shortage_notice_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "shortage_notice_outbox_id" not in columns:
        op.add_column("users", sa.Column("shortage_notice_outbox_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("users", "shortage_notice_outbox_id")
    op.drop_column("users", "shortage_notice_enabled")
    op.drop_column("orders", "enroute_notice_outbox_id")
