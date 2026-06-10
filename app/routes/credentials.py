import fnmatch
import json
import secrets
import string

import bcrypt
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.auth import admin_required
from app.database import get_db, get_request_db
import app.email_storage as email_storage
from app.models import EmailLog, SmtpCredential
import app.config as config
import app.graph as graph


def _matches_any(address: str, patterns: list[str]) -> bool:
    lower = address.lower()
    return any(fnmatch.fnmatch(lower, p.lower()) for p in patterns)

bp = Blueprint("credentials", __name__)


def _parse_patterns(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _form_checkbox(name: str) -> bool:
    return request.form.get(name) == "1"


DEFAULT_PASSWORD_LENGTH = 32
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 72  # bcrypt input limit


def _parse_password_length(raw: str | None) -> int | None:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return None
    if MIN_PASSWORD_LENGTH <= value <= MAX_PASSWORD_LENGTH:
        return value
    return None


def _generate_password(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _credential_modal_data(cred: SmtpCredential) -> dict:
    senders = cred.get_allowed_senders()
    return {
        "id": cred.id,
        "username": cred.username,
        "description": cred.description or "",
        "senders": "\n".join(senders),
        "recipients": "\n".join(cred.get_allowed_recipients()),
        "legacyData": bool(cred.legacy_data),
        "legacyTls": bool(cred.legacy_tls),
        "passwordLength": cred.password_length or DEFAULT_PASSWORD_LENGTH,
        "storeOnly": bool(cred.store_only),
        "saveToSentItems": bool(cred.save_to_sent_items),
        "firstSender": senders[0] if senders else "",
    }


@bp.route("/credentials")
@admin_required
def index():
    db = get_request_db()
    credentials = db.query(SmtpCredential).order_by(SmtpCredential.username).all()
    new_cred = session.pop("new_credential", None)
    credentials_json = {str(c.id): _credential_modal_data(c) for c in credentials}
    return render_template(
        "credentials.html",
        credentials=credentials,
        credentials_json=credentials_json,
        new_cred=new_cred,
        legacy_tls_port=config.LEGACY_TLS_PORT,
        default_password_length=DEFAULT_PASSWORD_LENGTH,
        min_password_length=MIN_PASSWORD_LENGTH,
        max_password_length=MAX_PASSWORD_LENGTH,
    )


@bp.route("/credentials/new", methods=["POST"])
@admin_required
def create():
    db = get_request_db()
    username = request.form.get("username", "").strip()
    description = request.form.get("description", "").strip()
    senders = _parse_patterns(request.form.get("allowed_senders", ""))
    recipients = _parse_patterns(request.form.get("allowed_recipients", ""))

    if not username:
        flash("Username is required.", "danger")
        return redirect(url_for("credentials.index"))

    if db.query(SmtpCredential).filter_by(username=username).first():
        flash(f"Username '{username}' is already taken.", "danger")
        return redirect(url_for("credentials.index"))

    password_length = _parse_password_length(request.form.get("password_length"))
    if password_length is None:
        flash(
            f"Password length must be between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH}.",
            "danger",
        )
        return redirect(url_for("credentials.index"))

    legacy_data = _form_checkbox("legacy_data")
    legacy_tls = _form_checkbox("legacy_tls")
    plain_password = _generate_password(password_length)
    hashed = bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()

    cred = SmtpCredential(
        username=username,
        hashed_password=hashed,
        description=description,
        allowed_senders=json.dumps(senders),
        allowed_recipients=json.dumps(recipients),
        legacy_data=legacy_data,
        legacy_tls=legacy_tls,
        password_length=password_length,
        store_only=_form_checkbox("store_only"),
        save_to_sent_items=_form_checkbox("save_to_sent_items"),
    )
    db.add(cred)
    db.commit()

    # Store in session so it can be displayed exactly once after redirect
    session["new_credential"] = {"username": username, "password": plain_password}
    return redirect(url_for("credentials.index"))


@bp.route("/credentials/<int:cred_id>/edit", methods=["POST"])
@admin_required
def edit(cred_id: int):
    db = get_request_db()
    cred = db.get(SmtpCredential, cred_id)
    if cred is None:
        flash("Credential not found.", "danger")
        return redirect(url_for("credentials.index"))

    cred.description = request.form.get("description", "").strip()
    cred.allowed_senders = json.dumps(_parse_patterns(request.form.get("allowed_senders", "")))
    cred.allowed_recipients = json.dumps(_parse_patterns(request.form.get("allowed_recipients", "")))
    password_length = _parse_password_length(request.form.get("password_length"))
    if password_length is None:
        flash(
            f"Password length must be between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH}.",
            "danger",
        )
        return redirect(url_for("credentials.index"))

    cred.legacy_data = _form_checkbox("legacy_data")
    cred.legacy_tls = _form_checkbox("legacy_tls")
    cred.password_length = password_length
    cred.store_only = _form_checkbox("store_only")
    cred.save_to_sent_items = _form_checkbox("save_to_sent_items")
    db.commit()
    flash(f"Credential '{cred.username}' updated.", "success")
    return redirect(url_for("credentials.index"))


@bp.route("/credentials/<int:cred_id>/toggle", methods=["POST"])
@admin_required
def toggle(cred_id: int):
    db = get_request_db()
    cred = db.get(SmtpCredential, cred_id)
    if cred is None:
        flash("Credential not found.", "danger")
    else:
        cred.is_active = not cred.is_active
        db.commit()
        state = "activated" if cred.is_active else "deactivated"
        flash(f"Credential '{cred.username}' {state}.", "success")
    return redirect(url_for("credentials.index"))


@bp.route("/credentials/<int:cred_id>/delete", methods=["POST"])
@admin_required
def delete(cred_id: int):
    db = get_request_db()
    cred = db.get(SmtpCredential, cred_id)
    if cred is None:
        flash("Credential not found.", "danger")
    else:
        db.delete(cred)
        db.commit()
        flash(f"Credential '{cred.username}' deleted.", "success")
    return redirect(url_for("credentials.index"))


@bp.route("/credentials/<int:cred_id>/reset-password", methods=["POST"])
@admin_required
def reset_password(cred_id: int):
    db = get_request_db()
    cred = db.get(SmtpCredential, cred_id)
    if cred is None:
        flash("Credential not found.", "danger")
        return redirect(url_for("credentials.index"))

    plain_password = _generate_password(cred.password_length)
    cred.hashed_password = bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()
    db.commit()

    session["new_credential"] = {"username": cred.username, "password": plain_password}
    return redirect(url_for("credentials.index"))


@bp.route("/credentials/<int:cred_id>/test-email", methods=["POST"])
@admin_required
def test_email(cred_id: int):
    db = get_request_db()
    cred = db.get(SmtpCredential, cred_id)
    if cred is None:
        flash("Credential not found.", "danger")
        return redirect(url_for("credentials.index"))

    from_address = request.form.get("from_address", "").strip()
    to_address = request.form.get("to_address", "").strip()

    if not from_address or not to_address:
        flash("Both 'from' and 'to' addresses are required.", "danger")
        return redirect(url_for("credentials.index"))

    raw_eml = (
        f"From: {from_address}\r\n"
        f"To: {to_address}\r\n"
        f"Subject: SMTP Relay - Test Email\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"This is a test email sent from your SMTP relay to verify the configuration."
    ).encode()

    log_entry = EmailLog(
        credential_id=cred.id,
        credential_username=cred.username,
        from_addr=from_address,
        to_addrs=json.dumps([to_address]),
        subject="SMTP Relay - Test Email",
        status="failed",  # set to real status below
    )
    db.add(log_entry)
    db.flush()
    log_entry.eml_path = email_storage.write_eml(log_entry.id, raw_eml)
    db.commit()

    # Validate allow-lists and log the reason if blocked
    block_reason = None
    if not _matches_any(from_address, cred.get_allowed_senders()):
        block_reason = f"Sender '{from_address}' not in allowed senders list"
    elif not _matches_any(to_address, cred.get_allowed_recipients()):
        block_reason = f"Recipient '{to_address}' not in allowed recipients list"

    if block_reason:
        log_entry.error_message = block_reason
        cred.last_used_at = log_entry.received_at
        db.commit()
        flash(f"Blocked: {block_reason}", "danger")
        return redirect(url_for("credentials.index"))

    if not cred.forwards_mail():
        log_entry.status = "stored"
        cred.last_used_at = log_entry.received_at
        db.commit()
        flash(f"Test email stored (credential is in store-only mode).", "info")
        return redirect(url_for("credentials.index"))

    try:
        graph.send_mail(
            from_address,
            [to_address],
            raw_eml,
            save_to_sent_items=cred.save_to_sent_items,
        )
        log_entry.status = "sent"
        cred.last_used_at = log_entry.received_at
        cred.total_sent = (cred.total_sent or 0) + 1
        db.commit()
        flash(f"Test email sent from {from_address} to {to_address}.", "success")
    except Exception as exc:
        log_entry.status = "failed"
        log_entry.error_message = str(exc)
        cred.last_used_at = log_entry.received_at
        db.commit()
        flash(f"Test email failed: {exc}", "danger")

    return redirect(url_for("credentials.index"))
