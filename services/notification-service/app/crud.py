import logging
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from . import models

logger = logging.getLogger(__name__)


def send_notification(db: Session, order_id: str, notification_type: str) -> None:
    """'Sends' a notification by logging it and recording it. Real delivery
    (email/SMS/push) would go here -- this is a mock, but a mock that's
    idempotent and queryable, which is the part worth demonstrating."""
    try:
        db.add(models.Notification(order_id=order_id, notification_type=notification_type))
        db.commit()
        logger.info("NOTIFICATION SENT: order=%s type=%s", order_id, notification_type)
    except IntegrityError:
        db.rollback()  # already notified for this order/type -- redelivered event, ignore


def get_notifications(db: Session, order_id: str) -> list[models.Notification]:
    return db.query(models.Notification).filter(models.Notification.order_id == order_id).all()
