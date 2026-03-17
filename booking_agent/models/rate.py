from datetime import date

from pydantic import BaseModel


class Rate(BaseModel):
    room_id: str
    room_name: str = ""
    date: date
    price: float
    currency: str = "EUR"


class RateUpdate(BaseModel):
    room_id: str
    date_from: date
    date_to: date
    price: float
