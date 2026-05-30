import logging
from contextlib import contextmanager

from flask import g
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import app.config as config
from app.models import AppSetting, Base

logger = logging.getLogger(__name__)

engine = None
SessionLocal: sessionmaker | None = None

SETTING_DEFAULTS = {
    "retention_days": "30",
}


def init_db() -> None:
    global engine, SessionLocal

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.CERTS_DIR.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{config.DB_PATH}",
        connect_args={"check_same_thread": False},
    )

    # Enable WAL mode for better concurrent write support
    @event.listens_for(engine, "connect")
    def set_wal_mode(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    _migrate()
    _seed_defaults()
    logger.info(f"Database initialised at {config.DB_PATH}")


def _migrate() -> None:
    """Add columns introduced after initial release to existing databases."""
    migrations = [
        ("smtp_credentials", "total_sent",   "INTEGER NOT NULL DEFAULT 0"),
        ("smtp_credentials", "last_used_at", "DATETIME"),
    ]
    with engine.connect() as conn:
        for table, column, definition in migrations:
            existing = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
                logger.info("Migration: added column %s.%s", table, column)
        conn.execute(text("DELETE FROM app_settings WHERE key = 'max_message_size_mb'"))
        conn.commit()


def _seed_defaults() -> None:
    with get_db() as db:
        for key, value in SETTING_DEFAULTS.items():
            if not db.query(AppSetting).filter_by(key=key).first():
                db.add(AppSetting(key=key, value=value))
        db.commit()


@contextmanager
def get_db():
    """Context manager for DB sessions used outside Flask request context
    (background jobs, SMTP handler)."""
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_request_db() -> Session:
    """Return a DB session tied to the current Flask request context.
    The session is closed automatically at request teardown."""
    if "db" not in g:
        g.db = SessionLocal()
    return g.db


def close_request_db(_error=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def db_healthy() -> bool:
    try:
        with get_db() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
