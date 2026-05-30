from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.auth import admin_required
from app.database import get_request_db
from app.models import AppSetting

bp = Blueprint("settings", __name__)


def _get_all_settings(db) -> dict[str, str]:
    return {s.key: s.value for s in db.query(AppSetting).all()}


def _set_setting(db, key: str, value: str) -> None:
    setting = db.query(AppSetting).filter_by(key=key).first()
    if setting:
        setting.value = value
    else:
        db.add(AppSetting(key=key, value=value))


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def index():
    db = get_request_db()

    if request.method == "POST":
        errors = []

        retention_raw = request.form.get("retention_days", "").strip()
        try:
            retention = int(retention_raw)
            if retention < 1:
                raise ValueError
        except ValueError:
            errors.append("Retention days must be a positive integer.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
        else:
            _set_setting(db, "retention_days", str(retention))
            db.commit()
            flash("Settings saved.", "success")

    settings = _get_all_settings(db)
    return render_template("settings.html", settings=settings)
