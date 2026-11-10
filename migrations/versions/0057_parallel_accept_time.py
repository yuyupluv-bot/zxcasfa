"""Record the acceptance time of parallel reservations."""
from alembic import op
import sqlalchemy as sa

revision = "0057_parallel_accept_time"
down_revision = "0056_enroute_notice"
branch_labels = None
depends_on = None


def upgrade():
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("orders")}
    if "parallel_accepted_at" not in columns:
        op.add_column("orders", sa.Column("parallel_accepted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("orders", "parallel_accepted_at")
