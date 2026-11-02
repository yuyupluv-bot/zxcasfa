"""Add exclusive direct-driver selection holds.

Revision ID: 0043_direct_driver_selection
Revises: 0042_route_parallel_offer_state
"""
from alembic import op

revision = "0043_direct_driver_selection"
down_revision = "0042_route_parallel_offer_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS requested_driver_id "
        "INTEGER REFERENCES users(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_orders_requested_driver_id "
        "ON orders(requested_driver_id)"
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS driver_selections (
            id SERIAL PRIMARY KEY,
            passenger_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            driver_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_driver_selections_passenger_id "
        "ON driver_selections(passenger_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_driver_selections_driver_id "
        "ON driver_selections(driver_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_driver_selections_expires_at "
        "ON driver_selections(expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS driver_selections")
    op.execute("DROP INDEX IF EXISTS ix_orders_requested_driver_id")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS requested_driver_id")
