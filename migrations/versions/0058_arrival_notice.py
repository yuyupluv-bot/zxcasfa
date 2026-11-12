"""Track the passenger arrival notice until boarding."""
from alembic import op
import sqlalchemy as sa

revision = "0058_arrival_notice"
down_revision = "0057_parallel_accept_time"
branch_labels = None
depends_on = None


def upgrade():
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("orders")}
    if "arrival_notice_outbox_id" not in columns:
        op.add_column("orders", sa.Column("arrival_notice_outbox_id", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("orders", "arrival_notice_outbox_id")
