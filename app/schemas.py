from pydantic import BaseModel, EmailStr, Field


class WebhookIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    ticket_id: str = Field(min_length=1, max_length=128)
    customer_email: EmailStr
    message: str = Field(min_length=1)
    reply_url: str = Field(min_length=1)


class MockTicketIn(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=128)
    customer_email: EmailStr
    message: str = Field(min_length=1)
    # Generated when omitted; pass the same value to simulate a redelivery.
    event_id: str | None = Field(default=None, min_length=1, max_length=128)


class ReplyIn(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1)
