"""Separate resettable public numbers from permanent primary keys."""
from alembic import op

revision = "0047_public_numbers"
down_revision = "0046_customer_price_cars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS order_public_number_seq START WITH 1")
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS public_number INTEGER")
    op.execute("ALTER TABLE orders ALTER COLUMN public_number SET DEFAULT nextval('order_public_number_seq')")
    op.execute("UPDATE orders SET public_number=id WHERE public_number IS NULL")
    op.execute("SELECT setval('order_public_number_seq', COALESCE(MAX(public_number), 1), MAX(public_number) IS NOT NULL) FROM orders")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_public_number ON orders(public_number)")

    op.execute("CREATE SEQUENCE IF NOT EXISTS booking_public_number_seq START WITH 1")
    op.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS public_number INTEGER")
    op.execute("ALTER TABLE bookings ALTER COLUMN public_number SET DEFAULT nextval('booking_public_number_seq')")
    op.execute("UPDATE bookings SET public_number=id WHERE public_number IS NULL")
    op.execute("SELECT setval('booking_public_number_seq', COALESCE(MAX(public_number), 1), MAX(public_number) IS NOT NULL) FROM bookings")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_bookings_public_number ON bookings(public_number)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_bookings_public_number")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS public_number")
    op.execute("DROP SEQUENCE IF EXISTS booking_public_number_seq")
    op.execute("DROP INDEX IF EXISTS ux_orders_public_number")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS public_number")
    op.execute("DROP SEQUENCE IF EXISTS order_public_number_seq")
