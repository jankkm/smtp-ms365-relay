"""Store raw .eml files on disk; DB holds only metadata and eml_path."""

import logging
from pathlib import Path

import app.config as config

logger = logging.getLogger(__name__)


def eml_relative_path(email_id: int) -> str:
    return f"{email_id}.eml"


def _resolve_path(eml_path: str) -> Path:
    """Map a stored relative path to an absolute file under EMAILS_DIR."""
    name = Path(eml_path).name
    if name != eml_path:
        raise ValueError(f"Invalid eml_path: {eml_path!r}")
    resolved = (config.EMAILS_DIR / name).resolve()
    if not str(resolved).startswith(str(config.EMAILS_DIR.resolve())):
        raise ValueError(f"Invalid eml_path: {eml_path!r}")
    return resolved


def ensure_emails_dir() -> None:
    config.EMAILS_DIR.mkdir(parents=True, exist_ok=True)


def write_eml(email_id: int, data: bytes) -> str:
    """Write raw MIME bytes for an email log row. Returns relative path for eml_path."""
    ensure_emails_dir()
    rel = eml_relative_path(email_id)
    path = config.EMAILS_DIR / rel
    path.write_bytes(data)
    return rel


def read_eml(eml_path: str | None) -> bytes | None:
    if not eml_path:
        return None
    try:
        path = _resolve_path(eml_path)
    except ValueError:
        logger.warning("Refusing to read invalid eml_path: %r", eml_path)
        return None
    if not path.is_file():
        return None
    return path.read_bytes()


def delete_eml(eml_path: str | None) -> None:
    if not eml_path:
        return
    try:
        path = _resolve_path(eml_path)
    except ValueError:
        logger.warning("Refusing to delete invalid eml_path: %r", eml_path)
        return
    if path.is_file():
        path.unlink()
