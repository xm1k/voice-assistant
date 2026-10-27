import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def now():
    return datetime.now(timezone.utc)


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


engine = create_engine(os.getenv("DOMAIN_DATABASE_URL", "sqlite:///./domain-v2.sqlite"), pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)
