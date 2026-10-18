import logging
import time

from psycopg import Error

from trafficxx.service import sweep


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    while True:
        try:
            count = sweep()
            if count:
                logging.info('Expired reservations: %d', count)
        except Error:
            logging.exception('Expiry sweep failed; retrying in five seconds')
        time.sleep(5)


if __name__ == '__main__':
    main()
