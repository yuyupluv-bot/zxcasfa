"""Private exclusion rules shared by every dispatch/claim path.

No passenger notifications and no global ban: the pair applies only to its owner.
"""
from common.models import DriverBlacklist, User


def excluded_driver_ids(session, passenger_id):
    passenger = session.get(User, passenger_id) if passenger_id else None
    if not passenger:
        return set()
    return {row[0] for row in session.query(DriverBlacklist.driver_id).filter(
        DriverBlacklist.passenger_vk_id == passenger.vk_id).all()}


def blocks(session, driver_id, passenger_id):
    return driver_id in excluded_driver_ids(session, passenger_id)


def add(session, driver, passenger_vk_id):
    # Serialize duplicate taps/add-from-rating with add-by-link on PostgreSQL.
    session.query(User).filter(User.id == driver.id).with_for_update().first()
    row = session.query(DriverBlacklist).filter_by(
        driver_id=driver.id, passenger_vk_id=passenger_vk_id).one_or_none()
    if row is None:
        row = DriverBlacklist(driver_id=driver.id, passenger_vk_id=passenger_vk_id)
        session.add(row)
        session.flush()
    return row
