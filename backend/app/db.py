from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.config import settings


class Base(DeclarativeBase):
    pass


_POOL = ({"pool_size": settings.db_pool_size, "max_overflow": settings.db_max_overflow, "pool_timeout": settings.db_pool_timeout_seconds, "pool_recycle": 1800}
         if settings.database_url.startswith("postgresql") else {})
engine = create_engine(settings.database_url, pool_pre_ping=True, **_POOL)


def get_db():
    with Session(engine) as session:
        yield session