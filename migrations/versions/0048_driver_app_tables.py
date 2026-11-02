"""Create persistent tables required by the Android driver application."""
from alembic import op

revision = "0048_driver_app_tables"
down_revision = "0047_public_numbers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        "CREATE TABLE IF NOT EXISTS driver_push_devices (id SERIAL PRIMARY KEY, driver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, token TEXT NOT NULL UNIQUE, platform VARCHAR(20) NOT NULL DEFAULT 'android', is_active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE INDEX IF NOT EXISTS ix_driver_push_devices_driver_id ON driver_push_devices(driver_id)",
        "CREATE TABLE IF NOT EXISTS push_scenarios (id SERIAL PRIMARY KEY, event_key VARCHAR(40) NOT NULL UNIQUE, title VARCHAR(120) NOT NULL, sound_file VARCHAR(255), duration_seconds INTEGER NOT NULL DEFAULT 60, repeat_seconds INTEGER NOT NULL DEFAULT 5, vibrate BOOLEAN NOT NULL DEFAULT TRUE, enabled BOOLEAN NOT NULL DEFAULT TRUE, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE IF NOT EXISTS driver_app_pair_codes (id SERIAL PRIMARY KEY, driver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, code_hash VARCHAR(64) NOT NULL UNIQUE, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE INDEX IF NOT EXISTS ix_driver_app_pair_codes_driver_id ON driver_app_pair_codes(driver_id)",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_driver_app_pair_codes_driver_id")
    op.execute("DROP TABLE IF EXISTS driver_app_pair_codes")
    op.execute("DROP TABLE IF EXISTS push_scenarios")
    op.execute("DROP INDEX IF EXISTS ix_driver_push_devices_driver_id")
    op.execute("DROP TABLE IF EXISTS driver_push_devices")
