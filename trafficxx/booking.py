from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, Response

from trafficxx.health import register
from trafficxx.models import ReservationRequest
from trafficxx.service import reservation, reserve

app = FastAPI(title='trafficXX Booking API', version='0.1.0')
register(app)
Customer = Annotated[str, Header(alias='X-Customer-Id', min_length=1, max_length=100)]
Key = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=200)]


@app.post('/reservations', status_code=201)
def create(body: ReservationRequest, response: Response, customer: Customer, key: Key):
    result, created = reserve(customer, key, body)
    response.status_code = 201 if created else 200
    return result


@app.get('/reservations/{reservation_id}')
def get(reservation_id: UUID, customer: Customer):
    return reservation(customer, reservation_id)


@app.post('/reservations/{reservation_id}/confirm')
def confirm(reservation_id: UUID, customer: Customer):
    return reservation(customer, reservation_id, 'CONFIRMED')


@app.post('/reservations/{reservation_id}/cancel')
def cancel(reservation_id: UUID, customer: Customer):
    return reservation(customer, reservation_id, 'CANCELLED')
