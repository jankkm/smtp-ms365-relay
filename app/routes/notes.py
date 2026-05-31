import markdown
from flask import Blueprint, flash, redirect, render_template, request, url_for
from markupsafe import Markup

from app.auth import admin_required
from app.database import get_request_db
from app.models import AppSetting

bp = Blueprint("notes", __name__)

_MARKDOWN_EXTENSIONS = ["fenced_code", "tables", "nl2br"]


def _get_notes(db) -> str:
    setting = db.query(AppSetting).filter_by(key="notes").first()
    return setting.value if setting else ""


def _set_notes(db, value: str) -> None:
    setting = db.query(AppSetting).filter_by(key="notes").first()
    if setting:
        setting.value = value
    else:
        db.add(AppSetting(key="notes", value=value))


def _render_notes(text: str) -> Markup:
    if not text.strip():
        return Markup("")
    return Markup(markdown.markdown(text, extensions=_MARKDOWN_EXTENSIONS))


@bp.route("/notes")
@admin_required
def index():
    db = get_request_db()
    notes = _get_notes(db)
    return render_template("notes.html", notes=notes, notes_html=_render_notes(notes))


@bp.route("/notes", methods=["POST"])
@admin_required
def save():
    db = get_request_db()
    _set_notes(db, request.form.get("notes", ""))
    db.commit()
    flash("Notes saved.", "success")
    return redirect(url_for("notes.index"))
