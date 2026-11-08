"""Track author and verified message visibility for common-chat notices."""
from alembic import op
import sqlalchemy as sa

revision = '0055_chat_notice_author'
down_revision = '0054_common_chat_notices'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {col['name'] for col in sa.inspect(bind).get_columns('common_chat_notices')}
    if 'author_vk_id' not in columns:
        op.add_column('common_chat_notices', sa.Column('author_vk_id', sa.BigInteger(), nullable=True))
    if 'seen_in_vk' not in columns:
        op.add_column('common_chat_notices', sa.Column('seen_in_vk', sa.Boolean(), nullable=False, server_default=sa.false()))
    indexes = {idx['name'] for idx in sa.inspect(bind).get_indexes('common_chat_notices')}
    if 'ix_common_chat_notices_author_vk_id' not in indexes:
        op.create_index('ix_common_chat_notices_author_vk_id', 'common_chat_notices', ['author_vk_id'])


def downgrade():
    op.drop_index('ix_common_chat_notices_author_vk_id', table_name='common_chat_notices')
    op.drop_column('common_chat_notices', 'seen_in_vk')
    op.drop_column('common_chat_notices', 'author_vk_id')
