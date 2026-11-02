"""Customer-price negotiation and two-car lifecycle labels."""
from alembic import op

revision = "0046_customer_price_cars"
down_revision = "0045_parallel_notice_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_price NUMERIC(10,2)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS agreed_price NUMERIC(10,2)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS driver_proposed_price NUMERIC(10,2)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_offer_driver_id INTEGER REFERENCES users(id)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS price_offer_outbox_id INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS multi_car_group_id INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_orders_price_offer_driver_id ON orders(price_offer_driver_id)",
        "CREATE INDEX IF NOT EXISTS ix_orders_multi_car_group_id ON orders(multi_car_group_id)",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_orders_multi_car_group_id")
    op.execute("DROP INDEX IF EXISTS ix_orders_price_offer_driver_id")
    for column in (
        "multi_car_group_id", "price_offer_outbox_id", "price_offer_driver_id",
        "driver_proposed_price", "agreed_price", "customer_price",
    ):
        op.execute(f"ALTER TABLE orders DROP COLUMN IF EXISTS {column}")
