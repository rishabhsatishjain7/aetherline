from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    sku: str
    name: str
    quantity_available: int = Field(ge=0)


class ProductOut(BaseModel):
    sku: str
    name: str
    quantity_available: int
    quantity_reserved: int

    class Config:
        from_attributes = True


class ReservationRequest(BaseModel):
    order_id: str
    sku: str
    quantity: int = Field(gt=0)


class ReservationResult(BaseModel):
    sku: str
    reserved: bool
    quantity_available: int
    reason: str | None = None
