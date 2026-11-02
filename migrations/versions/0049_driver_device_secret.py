"""Add a per-device credential for safe Firebase token rotation."""

from alembic import op
import sqlalchemy as sa


revision = "0049_driver_device_secret"
down_revision = "0048_driver_app_tables"
branch_labels = None
depends_on = None


def upgrade():
    # Revision 0001 creates the current ORM schema on a fresh database.
    # This field/index may therefore already exist before revision 0049.
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("driver_push_devices")}
    if "device_secret_hash" not in columns:
        op.add_column(
            "driver_push_devices",
            sa.Column("device_secret_hash", sa.String(length=64), nullable=True),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("driver_push_devices")}
    if "ix_driver_push_devices_device_secret_hash" not in indexes:
        op.create_index(
            "ix_driver_push_devices_device_secret_hash",
            "driver_push_devices",
            ["device_secret_hash"],
            unique=True,
        )


def downgrade():
    op.drop_index(
        "ix_driver_push_devices_device_secret_hash",
        table_name="driver_push_devices",
    )
    op.drop_column("driver_push_devices", "device_secret_hash")