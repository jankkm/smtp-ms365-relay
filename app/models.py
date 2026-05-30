import json
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import declarative_base

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
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    total_sent = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime, nullable=True)

    def get_allowed_senders(self) -> list[str]:
        return json.loads(self.allowed_senders or "[]")

    def get_allowed_recipients(self) -> list[str]:
        return json.loads(self.allowed_recipients or "[]")


class EmailLog(Base):
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True)
    credential_id = Column(Integer, ForeignKey("smtp_credentials.id", ondelete="SET NULL"), nullable=True)
    credential_username = Column(String)  # kept for display after credential deletion
    from_addr = Column(String)
    to_addrs = Column(Text)   # JSON list
    subject = Column(String)
    raw_eml = Column(LargeBinary)
    status = Column(String)   # "sent", "failed", "pending"
    error_message = Column(Text)
    received_at = Column(DateTime, default=datetime.utcnow)

    def get_to_addrs(self) -> list[str]:
        return json.loads(self.to_addrs or "[]")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)
