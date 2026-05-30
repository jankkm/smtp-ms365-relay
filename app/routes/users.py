from flask import Blueprint, flash, redirect, render_template, session, url_for

from app.auth import admin_required
from app.database import get_request_db
from app.models import User

bp = Blueprint("users", __name__)


@bp.route("/users")
@admin_required
def index():
    db = get_request_db()
    users = db.query(User).order_by(User.created_at).all()
    return render_template("users.html", users=users)


@bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@admin_required
def toggle_admin(user_id: int):
    db = get_request_db()
    user = db.get(User, user_id)
    if user is None:
        flash("User not found.", "danger")
        return redirect(url_for("users.index"))

    if user.id == session["user_id"]:
        flash("You cannot change your own admin status.", "warning")
        return redirect(url_for("users.index"))

    user.is_admin = not user.is_admin
    db.commit()
    state = "granted admin" if user.is_admin else "revoked admin"
    flash(f"{user.display_name or user.email}: {state}.", "success")
    return redirect(url_for("users.index"))
