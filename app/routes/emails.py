from datetime import datetime
import re

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from sqlalchemy.orm import load_only

from app.auth import admin_required
import app.email_storage as email_storage
import app.graph as graph
import app.mime_utils as mime_utils
from app.database import get_request_db
from app.models import EmailLog, SmtpCredential

EMAIL_LIST_COLUMNS = (
    EmailLog.id,
    EmailLog.credential_id,
    EmailLog.credential_username,
    EmailLog.from_addr,
    EmailLog.to_addrs,
    EmailLog.subject,
    EmailLog.status,
    EmailLog.error_message,
    EmailLog.received_at,
)

bp = Blueprint("emails", __name__)

PER_PAGE = 25
ENCODED_WORD_RE = re.compile(r"=\?.+\?=", re.IGNORECASE)


def _subject_needs_refresh(subject: str | None) -> bool:
    if not subject:
        return True
    return "\ufffd" in subject or bool(ENCODED_WORD_RE.search(subject))


@bp.route("/emails")
@admin_required
def index():
    db = get_request_db()

    cred_id = request.args.get("credential_id", type=int)
    status_filter = request.args.get("status", "")
    page = max(1, request.args.get("page", 1, type=int))

    query = db.query(EmailLog).order_by(EmailLog.received_at.desc())
    if cred_id:
        query = query.filter(EmailLog.credential_id == cred_id)
    if status_filter:
        query = query.filter(EmailLog.status == status_filter)

    total = query.count()
    emails = (
        query.options(load_only(*EMAIL_LIST_COLUMNS))
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
        .all()
    )
    dirty_subjects = False
    for entry in emails:
        if not _subject_needs_refresh(entry.subject):
            continue
        raw_eml = entry.read_raw_eml()
        if raw_eml is None:
            continue
        decoded = mime_utils.extract_subject(raw_eml)
        if decoded != (entry.subject or ""):
            entry.subject = decoded
            dirty_subjects = True
    if dirty_subjects:
        db.commit()

    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)

    credentials = db.query(SmtpCredential).order_by(SmtpCredential.username).all()

    return render_template(
        "emails.html",
        emails=emails,
        credentials=credentials,
        page=page,
        total_pages=total_pages,
        total=total,
        cred_id=cred_id,
        status_filter=status_filter,
    )


@bp.route("/emails/<int:email_id>/download")
@admin_required
def download(email_id: int):
    db = get_request_db()
    entry = db.get(EmailLog, email_id)
    if entry is None:
        abort(404)
    raw_eml = entry.read_raw_eml()
    if raw_eml is None:
        abort(404)
    return Response(
        raw_eml,
        mimetype="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="email_{email_id}.eml"'},
    )


@bp.route("/emails/<int:email_id>/resend", methods=["POST"])
@admin_required
def resend(email_id: int):
    db = get_request_db()
    entry = db.get(EmailLog, email_id)
    if entry is None:
        abort(404)

    raw_eml = entry.read_raw_eml()
    if raw_eml is None:
        flash("Cannot resend: raw message is missing.", "danger")
        return redirect(request.referrer or url_for("emails.index"))

    to_addrs = entry.get_to_addrs()
    save_to_sent_items = False
    if entry.credential_id:
        cred = db.get(SmtpCredential, entry.credential_id)
        if cred:
            save_to_sent_items = cred.save_to_sent_items
    try:
        graph.send_mail(
            entry.from_addr,
            to_addrs,
            raw_eml,
            save_to_sent_items=save_to_sent_items,
        )
        entry.status = "sent"
        entry.error_message = None
        if entry.credential_id:
            cred = db.get(SmtpCredential, entry.credential_id)
            if cred:
                cred.last_used_at = datetime.utcnow()
                cred.total_sent = (cred.total_sent or 0) + 1
        db.commit()
        flash(f"Email resent to {', '.join(to_addrs)}.", "success")
    except Exception as exc:
        entry.status = "failed"
        entry.error_message = str(exc)
        db.commit()
        flash(f"Resend failed: {exc}", "danger")

    return redirect(request.referrer or url_for("emails.index"))


@bp.route("/emails/<int:email_id>/delete", methods=["POST"])
@admin_required
def delete(email_id: int):
    db = get_request_db()
    entry = db.get(EmailLog, email_id)
    if entry is None:
        abort(404)

    subject = entry.subject or "(no subject)"
    email_storage.delete_eml(entry.eml_path)
    db.delete(entry)
    db.commit()
    flash(f"Deleted email: {subject}", "success")
    return redirect(request.referrer or url_for("emails.index"))
