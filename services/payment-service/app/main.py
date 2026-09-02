from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from prometheus_fastapi_instrumentator import Instrumentator

from . import models, crud
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Aetherline - Payment Service")
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-service"}


@app.get("/payments/{order_id}")
def get_payment(order_id: str, db: Session = Depends(get_db)):
    payment = crud.get_payment(db, order_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="payment_not_found")
    return {"order_id": payment.order_id, "status": payment.status, "created_at": payment.created_at}
