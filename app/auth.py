import logging
from functools import wraps

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, redirect, request, session, url_for

from app.database import get_db, get_request_db
from app.models import User

logger = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)
oauth = OAuth()


def init_oauth(app) -> None:
    import app.config as config

    oauth.init_app(app)
    oauth.register(
        name="entra",
        client_id=config.ENTRA_CLIENT_ID,
        client_secret=config.ENTRA_CLIENT_SECRET,
        server_metadata_url=(
            f"https://login.microsoftonline.com/{config.ENTRA_TENANT_ID}"
            f"/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("auth.login", next=request.path))
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/login")
def login():
    redirect_uri = url_for("auth.callback", _external=True)
    return oauth.entra.authorize_redirect(redirect_uri)


@bp.route("/auth/callback")
def callback():
    token = oauth.entra.authorize_access_token()
    userinfo = token.get("userinfo") or {}

    oid = userinfo.get("oid") or userinfo.get("sub")
    email = userinfo.get("email") or userinfo.get("preferred_username", "")
    display_name = userinfo.get("name", email)

    with get_db() as db:
        is_first_user = db.query(User).count() == 0
        user = db.query(User).filter_by(entra_oid=oid).first()

        if user is None:
            user = User(
                entra_oid=oid,
                email=email,
                display_name=display_name,
                is_admin=is_first_user,
            )
            db.add(user)
            logger.info(
                "New user registered: %s (admin=%s)", email, is_first_user
            )
        else:
            user.email = email
            user.display_name = display_name

        db.commit()

        session["user_id"] = user.id
        session["user_email"] = user.email
        session["user_name"] = user.display_name
        session["is_admin"] = user.is_admin

    next_url = request.args.get("next") or url_for("dashboard")
    return redirect(next_url)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
