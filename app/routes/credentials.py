import fnmatch
import json
import secrets

import bcrypt
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.auth import admin_required
from app.database import get_db, get_request_db
from app.models import EmailLog, SmtpCredential
import app.graph as graph


def _matches_any(address: str, patterns: list[str]) -> bool:
    lower = address.lower()
    return any(fnmatch.fnmatch(lower, p.lower()) for p in patterns)

bp = Blueprint("credentials", __name__)


def _parse_patterns(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _form_checkbox(name: str) -> bool:
    return request.form.get(name) == "1"


def _generate_password() -> str:
    return secrets.token_urlsafe(24)


@bp.route("/credentials")
@admin_required
def index():
    db = get_request_db()
    credentials = db.query(SmtpCredential).order_by(SmtpCredential.username).all()
    new_cred = session.pop("new_credential", None)
    return render_template("credentials.html", credentials=credentials, new_cred=new_cred)


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

    plain_password = _generate_password()
    hashed = bcrypt.hashpw(plain_password.encode(), bcrypt.gensalt()).decode()

    cred = SmtpCredential(
        username=username,
        hashed_password=hashed,
        description=description,
        allowed_senders=json.dumps(senders),
        allowed_recipients=json.dumps(recipients),
        legacy_data=_form_checkbox("legacy_data"),
        store_only=_form_checkbox("store_only"),
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
    cred.legacy_data = _form_checkbox("legacy_data")
    cred.store_only = _form_checkbox("store_only")
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

    plain_password = _generate_password()
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
        raw_eml=raw_eml,
        status="failed",  # set to real status below
    )
    db.add(log_entry)
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
        graph.send_mail(from_address, [to_address], raw_eml)
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
