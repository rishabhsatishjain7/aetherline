from fastapi import FastAPI, Depends, HTTPException, Header, Response
from sqlalchemy.orm import Session

from . import models, schemas, crud
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Aetherline - Order Service")
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "order-service"}


@app.post("/orders", response_model=schemas.OrderOut, status_code=202)
def place_order(
    order_in: schemas.OrderCreate,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """
    Returns 202 Accepted: the order is PENDING and its outcome (CONFIRMED or
    FAILED) resolves asynchronously via Kafka. Poll GET /orders/{id} or
    GET /orders/{id}/events, or consume from 'orders.confirmed'/'orders.failed'
    if you're another service. A retried request with the same
    Idempotency-Key returns the existing order at whatever status it's
    currently in -- if it's already resolved, that's a 200 rather than 202.
    """
    order = crud.create_order(db, order_in, idempotency_key)
    if order.status != models.OrderStatus.PENDING:
        response.status_code = 200
    return order


@app.get("/orders/{order_id}", response_model=schemas.OrderOut)
def get_order(order_id: str, db: Session = Depends(get_db)):
    order = crud.get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    return order


@app.post("/orders/{order_id}/cancel", response_model=schemas.OrderOut)
def cancel_order(order_id: str, db: Session = Depends(get_db)):
    order, error = crud.cancel_order(db, order_id)
    if error == "order_not_found":
        raise HTTPException(status_code=404, detail="order_not_found")
    if error == "order_already_cancelled":
        raise HTTPException(status_code=409, detail="order_already_cancelled")
    return order


@app.get("/orders/{order_id}/events", response_model=list[schemas.OrderEventOut])
def get_order_events(order_id: str, db: Session = Depends(get_db)):
    order = crud.get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    return order.events
