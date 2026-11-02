"""Add persistent rotating posts for a dedicated community chat."""

from alembic import op


revision = "0050_rotating_chat_posts"
down_revision = "0049_driver_device_secret"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS rotating_chat_posts ("
        "id SERIAL PRIMARY KEY, "
        "text TEXT NOT NULL DEFAULT '', "
        "attachments TEXT, "
        "position INTEGER NOT NULL DEFAULT 0, "
        "is_active BOOLEAN NOT NULL DEFAULT TRUE, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rotating_chat_posts_position "
        "ON rotating_chat_posts(position, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rotating_chat_posts_active "
        "ON rotating_chat_posts(is_active)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_rotating_chat_posts_active")
    op.execute("DROP INDEX IF EXISTS ix_rotating_chat_posts_position")
    op.execute("DROP TABLE IF EXISTS rotating_chat_posts")