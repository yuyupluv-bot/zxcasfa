"""Add non-working and weekly dispatcher marks to the driver schedule."""
from alembic import op
import sqlalchemy as sa

revision = "0061_driver_schedule_roles"
down_revision = "0060_driver_schedule"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("driver_schedule_off"):
        op.create_table(
            "driver_schedule_off",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "driver_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("schedule_date", sa.Date(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "driver_id",
                "schedule_date",
                name="uq_driver_schedule_off",
            ),
        )
        op.create_index(
            "ix_driver_schedule_off_driver_id",
            "driver_schedule_off",
            ["driver_id"],
        )
        op.create_index(
            "ix_driver_schedule_off_schedule_date",
            "driver_schedule_off",
            ["schedule_date"],
        )
    if not inspector.has_table("driver_schedule_dispatchers"):
        op.create_table(
            "driver_schedule_dispatchers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "dispatcher_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("week_start", sa.Date(), nullable=False, unique=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_driver_schedule_dispatchers_dispatcher_id",
            "driver_schedule_dispatchers",
            ["dispatcher_id"],
        )
        op.create_index(
            "ix_driver_schedule_dispatchers_week_start",
            "driver_schedule_dispatchers",
            ["week_start"],
        )


def downgrade():
    op.drop_table("driver_schedule_dispatchers")
    op.drop_table("driver_schedule_off")