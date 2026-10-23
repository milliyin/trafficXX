"""Booking owns a separate pool and independent capacity setting."""
from trafficxx.database_pool import DatabasePool


class BookingDatabase(DatabasePool):
    def __init__(self):
        super().__init__('booking', 'BOOKING_DB_POOL_MAX')
