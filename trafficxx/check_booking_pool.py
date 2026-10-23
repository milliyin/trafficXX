"""Read-only isolated Booking-identity pool diagnostic; no reservations created."""
import json

from trafficxx.booking_db import BookingDatabase
from trafficxx.check_browse_pool import run


if __name__ == '__main__':
    try:
        print(json.dumps(run(BookingDatabase, 'BOOKING_DB_POOL_MAX',
                             'trafficxx-lab-booking', 'BOOKING_POOL_OK')))
    except Exception as error:
        print(json.dumps({'result': 'BOOKING_POOL_FAILED', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
