"""One driver transaction at a time across VK and mobile command sources."""
from sqlalchemy import text

def acquire(session, vk_id):
    if session.get_bind().dialect.name=='postgresql':
        session.execute(text('SELECT pg_advisory_xact_lock(:lock_id)'),
                        {'lock_id': -(180_050_000_000_000 + int(vk_id))})
