"""Private per-driver passenger blacklist."""
from alembic import op
import sqlalchemy as sa
revision = "0059_driver_blacklist"
down_revision = "0058_arrival_notice"
branch_labels = None
depends_on = None

def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("orders")}
    if "eta_offer_source" not in columns:
        op.add_column("orders", sa.Column("eta_offer_source", sa.String(20), nullable=True))
    if "eta_offer_expires_at" not in columns:
        op.add_column("orders", sa.Column("eta_offer_expires_at", sa.DateTime(timezone=True), nullable=True))
    if inspector.has_table("driver_blacklist"):
        return
    op.create_table("driver_blacklist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("passenger_vk_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("driver_id", "passenger_vk_id", name="uq_driver_blacklist_pair"))
    op.create_index("ix_driver_blacklist_driver_id", "driver_blacklist", ["driver_id"])
    op.create_index("ix_driver_blacklist_passenger_vk_id", "driver_blacklist", ["passenger_vk_id"])

def downgrade():
    op.drop_table("driver_blacklist")
    op.drop_column("orders", "eta_offer_expires_at")
    op.drop_column("orders", "eta_offer_source")
