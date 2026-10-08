import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# gateway-db: the gateway's own database (database-per-service, same pattern
# as the other four services). Holds the `users` table for per-user JWT auth.
DATABASE_URL = os.getenv(
    "GATEWAY_DATABASE_URL",
    "postgresql://aetherline:aetherline@gateway-db:5432/gateway",
)

# Same engine settings as the other services: the default pool (5 + 10
# overflow) was measured to bottleneck under concurrent load.
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
