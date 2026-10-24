from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, Response
from fastapi.responses import PlainTextResponse

from trafficxx.health import register
from trafficxx.booking_db import BookingDatabase
from trafficxx.models import ReservationRequest
from trafficxx.pool_metrics import render
from trafficxx.service import reservation, reserve

database = BookingDatabase()


@asynccontextmanager
async def lifespan(app):
    database.open()
    try:
        yield
    finally:
        database.close()


app = FastAPI(title='trafficXX Booking API', version='0.1.0', lifespan=lifespan)
register(app, connection_factory=database.connection)
Customer = Annotated[str, Header(alias='X-Customer-Id', min_length=1, max_length=100)]
Key = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=200)]


@app.get('/internal/database-pool', include_in_schema=False)
def database_pool():
    return database.statistics()


@app.get('/metrics', include_in_schema=False, response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(render(database.statistics()['statistics'], component='booking'),
                             media_type='text/plain; version=0.0.4')


@app.post('/reservations', status_code=201)
def create(body: ReservationRequest, response: Response, customer: Customer, key: Key):
    result, created = reserve(customer, key, body, connection_factory=database.connection)
    response.status_code = 201 if created else 200
    return result


@app.get('/reservations/{reservation_id}')
def get(reservation_id: UUID, customer: Customer):
    return reservation(customer, reservation_id, connection_factory=database.connection)


@app.post('/reservations/{reservation_id}/confirm')
def confirm(reservation_id: UUID, customer: Customer):
    return reservation(customer, reservation_id, 'CONFIRMED', connection_factory=database.connection)


@app.post('/reservations/{reservation_id}/cancel')
def cancel(reservation_id: UUID, customer: Customer):
    return reservation(customer, reservation_id, 'CANCELLED', connection_factory=database.connection)
