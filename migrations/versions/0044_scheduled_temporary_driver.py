"""Schedule temporary driver-role activation for an exact local datetime.

Revision ID: 0044_scheduled_temporary_driver
Revises: 0043_direct_driver_selection
"""
from alembic import op

revision = "0044_scheduled_temporary_driver"
down_revision = "0043_direct_driver_selection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "temporary_driver_starts_at TIMESTAMPTZ"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_users_temporary_driver_starts_at "
        "ON users(temporary_driver_starts_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_temporary_driver_starts_at")
    op.execute(
        "ALTER TABLE users DROP COLUMN IF EXISTS temporary_driver_starts_at"
    )
