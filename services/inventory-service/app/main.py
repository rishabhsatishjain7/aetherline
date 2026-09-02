from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from prometheus_fastapi_instrumentator import Instrumentator

from . import models, schemas, crud
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Aetherline - Inventory Service")
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "inventory-service"}


@app.post("/products", response_model=schemas.ProductOut, status_code=201)
def create_product(product: schemas.ProductCreate, db: Session = Depends(get_db)):
    if crud.get_product(db, product.sku):
        raise HTTPException(status_code=409, detail="sku_already_exists")
    return crud.create_product(db, product)


@app.get("/products/{sku}", response_model=schemas.ProductOut)
def get_product(sku: str, db: Session = Depends(get_db)):
    product = crud.get_product(db, sku)
    if product is None:
        raise HTTPException(status_code=404, detail="sku_not_found")
    return product


@app.post("/inventory/reserve", response_model=schemas.ReservationResult)
def reserve(req: schemas.ReservationRequest, db: Session = Depends(get_db)):
    result = crud.reserve_stock(db, req)
    if not result.reserved:
        status_code = 404 if result.reason == "sku_not_found" else 409
        raise HTTPException(status_code=status_code, detail=result.reason)
    return result


@app.post("/inventory/release", response_model=schemas.ReservationResult)
def release(req: schemas.ReservationRequest, db: Session = Depends(get_db)):
    result = crud.release_stock(db, req)
    if not result.reserved:
        raise HTTPException(status_code=404, detail=result.reason)
    return result
