import logging
from contextlib import contextmanager

from flask import g
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import app.config as config
import app.email_storage as email_storage
from app.models import AppSetting, Base

logger = logging.getLogger(__name__)

engine = None
SessionLocal: sessionmaker | None = None

SETTING_DEFAULTS = {
    "retention_days": "30",
    "notes": "",
    "timezone": "UTC",
}


def init_db() -> None:
    global engine, SessionLocal

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.CERTS_DIR.mkdir(parents=True, exist_ok=True)
    email_storage.ensure_emails_dir()

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
        ("smtp_credentials", "legacy_data",  "BOOLEAN NOT NULL DEFAULT 0"),
        ("smtp_credentials", "legacy_tls",   "BOOLEAN NOT NULL DEFAULT 0"),
        ("smtp_credentials", "password_length", "INTEGER NOT NULL DEFAULT 32"),
        ("smtp_credentials", "mode",         "TEXT NOT NULL DEFAULT 'active'"),
        ("smtp_credentials", "store_only",   "BOOLEAN NOT NULL DEFAULT 0"),
        ("smtp_credentials", "save_to_sent_items", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    with engine.connect() as conn:
        for table, column, definition in migrations:
            existing = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
                logger.info("Migration: added column %s.%s", table, column)
        existing_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(smtp_credentials)"))]
        if "mode" in existing_cols and "store_only" in existing_cols:
            conn.execute(text(
                "UPDATE smtp_credentials SET store_only = 1 WHERE mode = 'store_only'"
            ))
            conn.execute(text(
                "UPDATE smtp_credentials SET is_active = 0 WHERE mode = 'inactive'"
            ))
        conn.execute(text("DELETE FROM app_settings WHERE key = 'max_message_size_mb'"))
        _migrate_email_storage(conn)
        conn.commit()


def _migrate_email_storage(conn) -> None:
    """Move legacy raw_eml blobs to disk and drop the BLOB column when possible."""
    tables = {
        row[0]
        for row in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
    }
    if "email_logs" not in tables:
        return

    cols = [row[1] for row in conn.execute(text("PRAGMA table_info(email_logs)"))]
    if "eml_path" not in cols:
        conn.execute(text("ALTER TABLE email_logs ADD COLUMN eml_path TEXT"))
        logger.info("Migration: added column email_logs.eml_path")
        cols.append("eml_path")

    email_storage.ensure_emails_dir()

    if "raw_eml" not in cols:
        return

    rows = conn.execute(
        text("SELECT id, raw_eml FROM email_logs WHERE raw_eml IS NOT NULL")
    ).fetchall()
    migrated = 0
    for row_id, blob in rows:
        if not blob:
            continue
        rel = email_storage.write_eml(row_id, blob)
        conn.execute(
            text("UPDATE email_logs SET eml_path = :path WHERE id = :id"),
            {"path": rel, "id": row_id},
        )
        migrated += 1
    if migrated:
        logger.info("Migration: moved %d email blob(s) from DB to disk", migrated)

    conn.execute(text("UPDATE email_logs SET raw_eml = NULL WHERE raw_eml IS NOT NULL"))
    try:
        conn.execute(text("ALTER TABLE email_logs DROP COLUMN raw_eml"))
        logger.info("Migration: dropped column email_logs.raw_eml")
    except Exception as exc:
        logger.warning(
            "Migration: could not drop email_logs.raw_eml (%s); "
            "column left empty in DB — upgrade SQLite or run VACUUM to reclaim space",
            exc,
        )


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
