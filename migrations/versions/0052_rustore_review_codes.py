"""Add revocable reusable pairing codes for store moderation."""

from alembic import op


revision = "0052_rustore_review_codes"
down_revision = "0051_outbox_dont_parse_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE driver_app_pair_codes "
        "ADD COLUMN IF NOT EXISTS is_reusable "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE driver_app_pair_codes "
        "ADD COLUMN IF NOT EXISTS label VARCHAR(120)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE driver_app_pair_codes DROP COLUMN IF EXISTS label"
    )
    op.execute(
        "ALTER TABLE driver_app_pair_codes DROP COLUMN IF EXISTS is_reusable"
    )