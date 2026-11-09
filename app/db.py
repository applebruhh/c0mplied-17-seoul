from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kw = {}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    eng = create_engine(url, future=True, **kw)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragma(conn, _):  # noqa: ANN001
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=3000")
            cur.close()
    return eng


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(eng=None) -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(eng or engine)


def rebind(url: str) -> None:
    """테스트용: 다른 DB로 갈아끼운다."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
    init_db(engine)


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
