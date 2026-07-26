"""Database connection for the Infra Portal.

Reads DATABASE_URL (set by docker-compose) and hands out SQLAlchemy sessions.
The whole app shares one set of typed models (ARCHITECTURE.md P6).
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# docker-compose provides this; the default lets you run scripts by hand too.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://infra_portal:devlocal_infra_portal_pw@localhost:5432/infra_portal",
)

# Normalise the older "postgresql://" form to the psycopg v3 driver we use.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)


class Base(DeclarativeBase):
    """Base class every ORM model inherits from."""


engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
