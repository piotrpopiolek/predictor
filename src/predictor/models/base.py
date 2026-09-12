"""SQLAlchemy declarative base. Domain tables are added in later waves."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
