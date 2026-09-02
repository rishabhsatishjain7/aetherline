from pydantic import BaseModel, Field
from datetime import datetime
from .models import OrderStatus


class OrderItemIn(BaseModel):
    sku: str
    quantity: int = Field(gt=0)


class OrderCreate(BaseModel):
    items: list[OrderItemIn]


class OrderItemOut(BaseModel):
    sku: str
    quantity: int

    class Config:
        from_attributes = True


class OrderOut(BaseModel):
    id: str
    status: OrderStatus
    created_at: datetime
    items: list[OrderItemOut]

    class Config:
        from_attributes = True


class OrderEventOut(BaseModel):
    event_type: str
    payload: str | None
    created_at: datetime

    class Config:
        from_attributes = True
