from flask import Blueprint, Response, abort, render_template, request

from app.auth import login_required
from app.database import get_request_db
from app.models import EmailLog, SmtpCredential

bp = Blueprint("emails", __name__)

PER_PAGE = 25


@bp.route("/emails")
@login_required
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
@login_required
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
