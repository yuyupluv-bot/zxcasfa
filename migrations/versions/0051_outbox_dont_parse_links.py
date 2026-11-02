"""Add per-message link-preview suppression to the VK outbox."""

from alembic import op


revision = "0051_outbox_dont_parse_links"
down_revision = "0050_rotating_chat_posts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE outbox_messages "
        "ADD COLUMN IF NOT EXISTS dont_parse_links "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE outbox_messages "
        "DROP COLUMN IF EXISTS dont_parse_links"
    )