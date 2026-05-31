import os
from pathlib import Path


def _env(var: str, default: str | None = None) -> str | None:
    """Read a config value from an env var or, if <VAR>_FILE is set, from that file."""
    if file_path := os.getenv(f"{var}_FILE"):
        return Path(file_path).read_text().strip()
    return os.getenv(var, default)


ENTRA_TENANT_ID: str = _env("ENTRA_TENANT_ID", "")
ENTRA_CLIENT_ID: str = _env("ENTRA_CLIENT_ID", "")
ENTRA_CLIENT_SECRET: str = _env("ENTRA_CLIENT_SECRET", "")

SECRET_KEY: str = _env("SECRET_KEY", "change-me-in-production")

LOG_LEVEL: str = (_env("LOG_LEVEL", "INFO") or "INFO").upper()

DATA_DIR: Path = Path(_env("DATA_DIR", "/app/data"))
CERTS_DIR: Path = DATA_DIR / "certs"
EMAILS_DIR: Path = DATA_DIR / "emails"
DB_PATH: Path = DATA_DIR / "smtp_relay.db"

SMTP_HOSTNAME: str = _env("SMTP_HOSTNAME", "localhost")

# Optional custom TLS certificate for SMTP. If both are set, auto-generation
# and auto-renewal are disabled and these paths are used directly.
SMTP_CERT_FILE: str | None = _env("SMTP_CERT_FILE")
SMTP_KEY_FILE: str | None = _env("SMTP_KEY_FILE")

MAX_MESSAGE_SIZE_MB: int = max(1, int(_env("MAX_MESSAGE_SIZE_MB", "35") or "35"))
MAX_MESSAGE_SIZE_BYTES: int = MAX_MESSAGE_SIZE_MB * 1024 * 1024
