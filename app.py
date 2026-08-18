"""
Flask application factory.

Usage::

    # Development
    python app.py

    # Production (Gunicorn)
    gunicorn "app:create_app()" --bind 0.0.0.0:8000

Authentication note
-------------------
Phase 1 uses a simple session placeholder.  The application is structured so
that Azure Easy Auth or Microsoft Entra ID authentication can be added later
by injecting the authenticated user into ``flask.g`` or ``flask.session``
without changing any business logic.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, g, render_template, session

load_dotenv()  # Load .env file if present

from config import get_config
from routes.reconciliation_routes import bp as reconciliation_bp
from routes.setup_routes import bp as setup_bp
from services.database import init_db


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app(config_class=None) -> Flask:
    """Create and configure the Flask application.

    Args:
        config_class: A configuration class.  If ``None``, the class is
                      selected based on the ``FLASK_ENV`` environment variable.

    Returns:
        A configured ``Flask`` instance.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Load configuration
    cfg = config_class or get_config()
    app.config.from_object(cfg)

    # Harden session cookies
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Ensure upload / output directories exist
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["OUTPUT_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # Register blueprints
    app.register_blueprint(reconciliation_bp)
    app.register_blueprint(setup_bp)

    # Initialise database
    with app.app_context():
        init_db(app.config["DATABASE_PATH"])

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------

    @app.errorhandler(404)
    def not_found(exc):
        return render_template("errors/404.html"), 404

    @app.errorhandler(413)
    def too_large(exc):
        return render_template("errors/413.html"), 413

    @app.errorhandler(500)
    def server_error(exc):
        logger.error("Internal server error: %s", exc, exc_info=True)
        return render_template("errors/500.html"), 500

    # ------------------------------------------------------------------
    # Auth placeholder
    # ------------------------------------------------------------------

    @app.before_request
    def load_user():
        """Phase 1 placeholder: set a default user in session.

        Replace this block with Azure Easy Auth / Entra ID header inspection
        when authentication is enabled.
        """
        if "user" not in session:
            session["user"] = "web_user"

    logger.info(
        "Flask app created.  ENV=%s  DEBUG=%s",
        app.config.get("FLASK_ENV"),
        app.config.get("DEBUG"),
    )
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    application = create_app()
    application.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", 5000)),
        debug=application.config.get("DEBUG", False),
    )
