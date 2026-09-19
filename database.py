"""
Подключение к БД и сессии SQLAlchemy.

Раздел 9 ТЗ: PostgreSQL/Supabase в production, SQLite на этапе разработки.
Один и тот же код моделей работает с обоими диалектами: специфика SQLite
(check_same_thread, PRAGMA foreign_keys) изолирована здесь.
"""

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import settings


def _engine_kwargs() -> dict:
    kwargs: dict = {
        "echo": settings.db_echo,
        "future": True,
    }
    if settings.is_sqlite:
        # SQLite: разрешаем использование соединения из разных потоков,
        # т.к. FastAPI выполняет sync-эндпоинты в threadpool.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # PostgreSQL: проверка «живости» соединения перед выдачей из пула —
        # нужна при хостинге с обрывами простаивающих соединений (Supabase/Render).
        kwargs["pool_pre_ping"] = True
    return kwargs


engine = create_engine(settings.database_url, **_engine_kwargs())


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    """SQLite по умолчанию игнорирует FOREIGN KEY — включаем принудительно,
    иначе поведение разработки расходится с PostgreSQL."""
    if settings.is_sqlite:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    """Базовый класс декларативных моделей."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI-зависимость: сессия на запрос, гарантированное закрытие."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
