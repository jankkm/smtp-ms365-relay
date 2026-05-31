"""Application entry point and startup orchestration.

Startup order:
  1. TLS certificate check / generation
  2. Database initialisation
  3. Email cleanup (immediate, then scheduled daily)
  4. APScheduler background jobs
  5. SMTP server (background threads via aiosmtpd Controller)
  6. Flask app (served by gunicorn)
"""

import logging
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, redirect, render_template, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import func

import app.cert as cert_module
import app.config as config
import app.smtp_server as smtp_server
from app.auth import admin_required, init_oauth, login_required
from app.auth import bp as auth_bp
from app.database import close_request_db, db_healthy, get_db, get_request_db, init_db
from app.models import AppSetting, EmailLog, SmtpCredential
from app.routes.credentials import bp as credentials_bp
from app.routes.emails import bp as emails_bp
from app.routes.notes import bp as notes_bp
from app.routes.settings import bp as settings_bp
from app.routes.users import bp as users_bp

_log_level = getattr(logging, config.LOG_LEVEL, None)
if not isinstance(_log_level, int):
    _log_level = logging.INFO

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)
if config.LOG_LEVEL not in logging.getLevelNamesMapping():
    logger.warning("Invalid LOG_LEVEL %r, using INFO", config.LOG_LEVEL)


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

def cleanup_old_emails() -> None:
    with get_db() as db:
        setting = db.query(AppSetting).filter_by(key="retention_days").first()
        retention_days = int(setting.value) if setting else 30
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        deleted = db.query(EmailLog).filter(EmailLog.received_at < cutoff).delete()
        db.commit()
    if deleted:
        logger.info("Email cleanup: removed %d entries older than %d days", deleted, retention_days)


def check_and_renew_cert() -> None:
    if config.SMTP_CERT_FILE and config.SMTP_KEY_FILE:
        if cert_module.custom_cert_changed():
            logger.info("Custom TLS certificate changed on disk — reloading SMTP SSL context.")
            try:
                cert_module.create_ssl_context()
            except Exception as exc:
                logger.error("Custom TLS cert/key invalid, skipping reload: %s", exc)
                return
            smtp_server.reload_ssl_context()
            cert_module.record_loaded_cert()
        return
    cert_module.ensure_certificate()
    smtp_server.reload_ssl_context()
    cert_module.record_loaded_cert()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _startup() -> None:
    logger.info("=== SMTP Relay starting up ===")
    cert_module.ensure_certificate()
    init_db()
    cleanup_old_emails()

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(cleanup_old_emails, "interval", hours=24, id="email_cleanup")
    scheduler.add_job(check_and_renew_cert, "interval", hours=24, id="cert_renewal")
    scheduler.start()
    logger.info("APScheduler started (email cleanup + cert renewal every 24 h)")

    smtp_server.start_smtp_server()
    cert_module.record_loaded_cert()
    logger.info("=== Startup complete ===")


_startup()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    flask_app = Flask(__name__)
    flask_app.secret_key = config.SECRET_KEY
    flask_app.wsgi_app = ProxyFix(flask_app.wsgi_app, x_proto=1, x_host=1)

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(credentials_bp)
    flask_app.register_blueprint(emails_bp)
    flask_app.register_blueprint(users_bp)
    flask_app.register_blueprint(notes_bp)
    flask_app.register_blueprint(settings_bp)

    flask_app.teardown_appcontext(close_request_db)
    init_oauth(flask_app)

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    @flask_app.route("/")
    @login_required
    def dashboard():
        if not session.get("is_admin"):
            return render_template("dashboard.html", access_denied=True)

        db = get_request_db()

        today = datetime.utcnow().date()
        week_ago = datetime.utcnow() - timedelta(days=7)

        emails_today_sent = (
            db.query(func.count(EmailLog.id))
            .filter(func.date(EmailLog.received_at) == today, EmailLog.status == "sent")
            .scalar() or 0
        )
        emails_today_failed = (
            db.query(func.count(EmailLog.id))
            .filter(func.date(EmailLog.received_at) == today, EmailLog.status == "failed")
            .scalar() or 0
        )
        emails_week = (
            db.query(func.count(EmailLog.id))
            .filter(EmailLog.received_at >= week_ago, EmailLog.status == "sent")
            .scalar() or 0
        )
        active_credentials = (
            db.query(func.count(SmtpCredential.id))
            .filter(SmtpCredential.is_active.is_(True))
            .scalar() or 0
        )

        # Per-credential stats for this week (only credentials with activity)
        from sqlalchemy import case as sa_case
        cred_stats = (
            db.query(
                SmtpCredential.username,
                func.count(EmailLog.id).label("total"),
                func.sum(
                    sa_case((EmailLog.status == "sent", 1), else_=0)
                ).label("sent"),
            )
            .join(EmailLog, EmailLog.credential_id == SmtpCredential.id)
            .filter(EmailLog.received_at >= week_ago)
            .group_by(SmtpCredential.id)
            .order_by(func.count(EmailLog.id).desc())
            .all()
        )

        recent_emails = (
            db.query(EmailLog)
            .order_by(EmailLog.received_at.desc())
            .limit(10)
            .all()
        )

        return render_template(
            "dashboard.html",
            access_denied=False,
            emails_today_sent=emails_today_sent,
            emails_today_failed=emails_today_failed,
            emails_week=emails_week,
            active_credentials=active_credentials,
            cred_stats=cred_stats,
            recent_emails=recent_emails,
        )

    # ------------------------------------------------------------------
    # Health endpoint (no auth required)
    # ------------------------------------------------------------------

    @flask_app.route("/health")
    def health():
        db_ok = db_healthy()
        smtp_ok = smtp_server.is_running()
        status = "ok" if db_ok and smtp_ok else "degraded"
        http_code = 200 if status == "ok" else 503
        return jsonify({"status": status, "db": "ok" if db_ok else "error",
                        "smtp": "ok" if smtp_ok else "error"}), http_code

    # ------------------------------------------------------------------
    # Error pages
    # ------------------------------------------------------------------

    @flask_app.errorhandler(403)
    def forbidden(_e):
        return render_template("403.html"), 403

    return flask_app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
