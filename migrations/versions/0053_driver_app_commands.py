"""Durable mobile commands; no changes to order/queue/timer tables."""
from alembic import op
import sqlalchemy as sa
revision='0053_driver_app_commands'
down_revision='0052_rustore_review_codes'
branch_labels=None
depends_on=None

def upgrade():
    # 0001 uses the current Base metadata, including this table on fresh installs.
    if sa.inspect(op.get_bind()).has_table('driver_app_commands'):
        return
    op.create_table('driver_app_commands',
      sa.Column('id',sa.Integer(),primary_key=True),
      sa.Column('driver_id',sa.Integer(),sa.ForeignKey('users.id',ondelete='CASCADE'),nullable=False),
      sa.Column('device_id',sa.Integer(),sa.ForeignKey('driver_push_devices.id',ondelete='CASCADE'),nullable=False),
      sa.Column('request_id',sa.String(36),nullable=False),sa.Column('revision',sa.String(64),nullable=False),
      sa.Column('body_hash',sa.String(64),nullable=False),sa.Column('body',sa.Text(),nullable=False),
      sa.Column('status',sa.String(20),nullable=False,server_default='pending'),sa.Column('error',sa.String(240)),
      sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
      sa.Column('expires_at',sa.DateTime(timezone=True),nullable=False),sa.Column('finished_at',sa.DateTime(timezone=True)),
      sa.UniqueConstraint('driver_id','request_id',name='uq_driver_app_request'))
    op.create_index('ix_driver_app_commands_driver_id','driver_app_commands',['driver_id'])
    op.create_index('ix_driver_app_commands_status','driver_app_commands',['status'])

def downgrade():
    op.drop_table('driver_app_commands')
