"""
Career Raider - Database Session Management
Connection pooling, session factory, and health check.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.exc import OperationalError, StatementError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from contextlib import contextmanager
from typing import Generator
import time

from src.config import get_settings
from src.logger import get_logger
from src.models.job import Base

log = get_logger("db")
_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,  # Automatically reconnect on stale connections
            pool_recycle=3600,   # Recycle connections every hour
            echo=False,
        )
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autocommit=False, autoflush=False)
    return _SessionLocal


@contextmanager
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((OperationalError, StatementError)),
    reraise=True
)
def get_db_session() -> Generator[Session, None, None]:
    """Context manager for safe DB transactions with exponential backoff for transient errors."""
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        # Log the retry if it's an operational error
        if isinstance(e, (OperationalError, StatementError)):
            log.warning("Database transient error, retrying...", error=str(e))
        raise
    finally:
        session.close()


def init_db():
    """Create all tables if they don't exist yet."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    log.info("Database tables initialized")


def check_db_health() -> bool:
    """Returns True if database is reachable."""
    try:
        with get_db_session() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception as e:
        log.error("Database health check failed", error=str(e))
        return False


def wait_for_db(max_retries: int = 10, delay: int = 3):
    """Block until DB is reachable (useful at startup)."""
    for attempt in range(max_retries):
        if check_db_health():
            log.info("Database is ready")
            return
        log.warning("Database not ready, retrying...", attempt=attempt+1, delay=delay)
        time.sleep(delay)
    raise RuntimeError("Database not reachable after multiple retries")
