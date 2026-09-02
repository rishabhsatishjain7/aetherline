from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from prometheus_fastapi_instrumentator import Instrumentator

from . import models, crud
from .database import engine, get_db

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Aetherline - Notification Service")
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "notification-service"}


@app.get("/notifications/{order_id}")
def get_notifications(order_id: str, db: Session = Depends(get_db)):
    notifications = crud.get_notifications(db, order_id)
    return [
        {"notification_type": n.notification_type, "created_at": n.created_at}
        for n in notifications
    ]
