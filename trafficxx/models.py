from pydantic import BaseModel, Field, model_validator


class ReservationRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    quantity: int | None = Field(default=None, ge=1, le=100, strict=True)
    seats: list[str] | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode='after')
    def validate_inventory(self):
        if (self.quantity is None) == (self.seats is None):
            raise ValueError('Supply either quantity or seats')
        if self.seats is not None:
            if any(not seat or len(seat) > 50 for seat in self.seats):
                raise ValueError('Seat labels must contain 1 to 50 characters')
            if len(set(self.seats)) != len(self.seats):
                raise ValueError('Duplicate seats are not allowed')
            self.seats = sorted(self.seats)
        return self
