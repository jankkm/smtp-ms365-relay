import fnmatch
import json
import logging
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header as _decode_header_raw

import bcrypt
from aiosmtpd.smtp import AuthResult, LoginPassword

import app.graph as graph
from app.database import get_db
from app.models import EmailLog, SmtpCredential

logger = logging.getLogger(__name__)


def _matches_any(address: str, patterns: list[str]) -> bool:
    """Return True if address matches at least one fnmatch pattern (case-insensitive)."""
    lower = address.lower()
    return any(fnmatch.fnmatch(lower, p.lower()) for p in patterns)


def _decode_subject(raw: str) -> str:
    parts = _decode_header_raw(raw or "")
    result = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            try:
                result.append(fragment.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                result.append(fragment.decode("utf-8", errors="replace"))
        else:
            result.append(fragment)
    return "".join(result)


class RelayAuthenticator:
    """Validates SMTP AUTH credentials against the database."""

    def __call__(self, server, session, envelope, mechanism, auth_data):
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=True)

        username = auth_data.login.decode("utf-8", errors="replace")
        password = auth_data.password.decode("utf-8", errors="replace")

        with get_db() as db:
            cred = (
                db.query(SmtpCredential)
                .filter_by(username=username, is_active=True)
                .first()
            )
            if cred is None:
                logger.warning("SMTP auth failed from %s: unknown user '%s'", session.peer[0], username)
                return AuthResult(success=False, handled=True)

            if not bcrypt.checkpw(password.encode(), cred.hashed_password.encode()):
                logger.warning("SMTP auth failed from %s: wrong password for '%s'", session.peer[0], username)
                return AuthResult(success=False, handled=True)

            # Capture allowed patterns in a plain dict so the session outlives the DB session
            auth_object = {
                "username": username,
                "credential_id": cred.id,
                "allowed_senders": cred.get_allowed_senders(),
                "allowed_recipients": cred.get_allowed_recipients(),
                "legacy_data": cred.legacy_data,
                "forwards_mail": cred.forwards_mail(),
            }

        logger.info("SMTP auth accepted: '%s'", username)
        return AuthResult(success=True, auth_data=auth_object)


def _log_blocked(auth: dict, from_addr: str, to_addrs: list[str],
                 subject: str, raw_eml: bytes, reason: str) -> None:
    """Persist a failed EmailLog entry for emails blocked by allow-list rules."""
    with get_db() as db:
        entry = EmailLog(
            credential_id=auth["credential_id"],
            credential_username=auth["username"],
            from_addr=from_addr,
            to_addrs=json.dumps(to_addrs),
            subject=subject,
            raw_eml=raw_eml,
            status="failed",
            error_message=reason,
        )
        db.add(entry)
        cred = db.get(SmtpCredential, auth["credential_id"])
        if cred:
            cred.last_used_at = datetime.utcnow()
        db.commit()


class RelayHandler:
    """Handles incoming SMTP DATA: validates rules, persists the email, and forwards via Graph."""

    async def handle_DATA(self, server, session, envelope):
        auth = session.auth_data
        if auth is None:
            return "530 5.7.0 Authentication required"

        from_addr: str = envelope.mail_from
        to_addrs: list[str] = list(envelope.rcpt_tos)
        raw_eml: bytes = (
            envelope.content
            if isinstance(envelope.content, bytes)
            else envelope.content.encode()
        )

        subject = _decode_subject(message_from_bytes(raw_eml).get("Subject", ""))

        if not _matches_any(from_addr, auth["allowed_senders"]):
            reason = f"Sender '{from_addr}' not in allowed senders list"
            logger.warning("%s (credential '%s')", reason, auth["username"])
            _log_blocked(auth, from_addr, to_addrs, subject, raw_eml, reason)
            return "550 5.7.1 Sender address not permitted"

        for addr in to_addrs:
            if not _matches_any(addr, auth["allowed_recipients"]):
                reason = f"Recipient '{addr}' not in allowed recipients list"
                logger.warning("%s (credential '%s')", reason, auth["username"])
                _log_blocked(auth, from_addr, to_addrs, subject, raw_eml, reason)
                return f"550 5.7.1 Recipient {addr} not permitted"

        # Persist the email immediately so it's logged even if delivery fails
        with get_db() as db:
            entry = EmailLog(
                credential_id=auth["credential_id"],
                credential_username=auth["username"],
                from_addr=from_addr,
                to_addrs=json.dumps(to_addrs),
                subject=subject,
                raw_eml=raw_eml,
                status="pending" if auth["forwards_mail"] else "stored",
            )
            db.add(entry)
            db.commit()
            log_id = entry.id

        if not auth["forwards_mail"]:
            with get_db() as db:
                cred = db.get(SmtpCredential, auth["credential_id"])
                if cred:
                    cred.last_used_at = datetime.utcnow()
                db.commit()
            logger.info("Email %d stored (not forwarded): %s -> %s", log_id, from_addr, to_addrs)
            return "250 2.0.0 OK"

        try:
            graph.send_mail(from_addr, to_addrs, raw_eml)
            status, error = "sent", None
            logger.info("Email %d forwarded via Graph: %s -> %s", log_id, from_addr, to_addrs)
        except Exception as exc:
            status, error = "failed", str(exc)
            logger.error("Email %d delivery failed: %s", log_id, exc)

        with get_db() as db:
            entry = db.get(EmailLog, log_id)
            if entry:
                entry.status = status
                entry.error_message = error

            cred = db.get(SmtpCredential, auth["credential_id"])
            if cred:
                cred.last_used_at = datetime.utcnow()
                if status == "sent":
                    cred.total_sent = (cred.total_sent or 0) + 1

            db.commit()

        if status == "sent":
            return "250 2.0.0 OK"
        return f"550 5.4.0 Delivery failed: {error}"
