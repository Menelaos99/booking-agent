from datetime import datetime

from pydantic import BaseModel


class Message(BaseModel):
    id: str
    guest_name: str
    subject: str
    date: datetime
    unread: bool = False
    preview: str = ""


class MessageDetail(Message):
    body: str
    reservation_id: str = ""
