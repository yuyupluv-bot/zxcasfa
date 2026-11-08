"""Track notices for passenger messages in the common VK chat."""
from alembic import op
import sqlalchemy as sa
revision = "0054_common_chat_notices"
down_revision = "0053_driver_app_commands"
branch_labels = None
depends_on = None

def upgrade():
    if sa.inspect(op.get_bind()).has_table("common_chat_notices"):
        return
    op.create_table("common_chat_notices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("peer_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_message_id", sa.BigInteger(), nullable=False),
        sa.Column("vk_message_id", sa.BigInteger()),
        sa.Column("outbox_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("answered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("peer_id", "conversation_message_id", name="uq_common_chat_notice"))

def downgrade():
    op.drop_table("common_chat_notices")
