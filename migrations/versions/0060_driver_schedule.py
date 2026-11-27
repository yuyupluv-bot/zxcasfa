"""Add an informational hourly schedule for drivers."""
from alembic import op
import sqlalchemy as sa

revision = "0060_driver_schedule"
down_revision = "0059_driver_blacklist"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("driver_schedule_marks"):
        return
    op.create_table(
        "driver_schedule_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "driver_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schedule_date", sa.Date(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "driver_id",
            "schedule_date",
            "hour",
            name="uq_driver_schedule_mark",
        ),
        sa.CheckConstraint(
            "hour >= 0 AND hour <= 23",
            name="ck_driver_schedule_hour",
        ),
    )
    op.create_index(
        "ix_driver_schedule_marks_driver_id",
        "driver_schedule_marks",
        ["driver_id"],
    )
    op.create_index(
        "ix_driver_schedule_marks_schedule_date",
        "driver_schedule_marks",
        ["schedule_date"],
    )


def downgrade():
    op.drop_table("driver_schedule_marks")