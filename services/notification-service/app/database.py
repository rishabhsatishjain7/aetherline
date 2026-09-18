import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://aetherline:aetherline@notification-db:5432/notifications"
)

# Default pool (5 connections + 10 overflow) was measured to bottleneck
# under concurrent load -- see load-test results. Sized up for local/single-
# instance deployment; a real production system would tune this per
# service based on actual traffic patterns and available DB connections.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=20,
    pool_timeout=30,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
