from datetime import date

from pydantic import BaseModel


class Reservation(BaseModel):
    booking_id: str
    guest_name: str
    check_in: date
    check_out: date
    status: str
    total: str
    room_type: str = ""
    num_guests: int = 0
    special_requests: str = ""


class ReservationDetail(Reservation):
    guest_email: str = ""
    guest_phone: str = ""
    payment_status: str = ""
    commission: str = ""
    notes: str = ""
