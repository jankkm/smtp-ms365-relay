from datetime import datetime

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from app.auth import admin_required
from app.database import get_request_db
from app.models import EmailLog, SmtpCredential
import app.graph as graph

bp = Blueprint("emails", __name__)

PER_PAGE = 25


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
    emails = query.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
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
    return Response(
        entry.raw_eml,
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

    if not entry.raw_eml:
        flash("Cannot resend: raw message is missing.", "danger")
        return redirect(request.referrer or url_for("emails.index"))

    to_addrs = entry.get_to_addrs()
    try:
        graph.send_mail(entry.from_addr, to_addrs, entry.raw_eml)
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
    db.delete(entry)
    db.commit()
    flash(f"Deleted email: {subject}", "success")
    return redirect(request.referrer or url_for("emails.index"))
