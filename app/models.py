import json
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base

from app import email_storage

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    entra_oid = Column(String, unique=True, nullable=False)
    email = Column(String, nullable=False)
    display_name = Column(String)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class SmtpCredential(Base):
    __tablename__ = "smtp_credentials"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    description = Column(String, default="")
    allowed_senders = Column(Text, default="[]")    # JSON list of patterns
    allowed_recipients = Column(Text, default="[]") # JSON list of patterns
    legacy_data = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    store_only = Column(Boolean, default=False, nullable=False)
    save_to_sent_items = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    total_sent = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    def get_allowed_senders(self) -> list[str]:
        return json.loads(self.allowed_senders or "[]")

    def get_allowed_recipients(self) -> list[str]:
        return json.loads(self.allowed_recipients or "[]")

    def forwards_mail(self) -> bool:
        return self.is_active and not self.store_only


class EmailLog(Base):
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True)
    credential_id = Column(Integer, ForeignKey("smtp_credentials.id", ondelete="SET NULL"), nullable=True)
    credential_username = Column(String)  # kept for display after credential deletion
    from_addr = Column(String)
    to_addrs = Column(Text)   # JSON list
    subject = Column(String)
    eml_path = Column(String)  # relative path under DATA_DIR/emails, e.g. "42.eml"
    status = Column(String)   # "sent", "failed", "pending", "stored"
    error_message = Column(Text)
    received_at = Column(DateTime, default=datetime.utcnow)

    def get_to_addrs(self) -> list[str]:
        return json.loads(self.to_addrs or "[]")

    def read_raw_eml(self) -> bytes | None:
        return email_storage.read_eml(self.eml_path)

    def has_raw_eml(self) -> bool:
        return self.read_raw_eml() is not None


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
