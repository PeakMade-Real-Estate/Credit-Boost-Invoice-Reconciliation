"""
Application configuration.

All secrets and environment-specific values are loaded from environment
variables.  Never hardcode secrets in this file.

Usage in other modules::

    from config import Config
    tolerance = Config.INVOICE_TOLERANCE
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).parent


class Config:
    """Base configuration – values are overridden by environment variables."""

    # ------------------------------------------------------------------
    # Flask core
    # ------------------------------------------------------------------
    SECRET_KEY: str = os.environ.get(
        "SECRET_KEY", "dev-secret-key-CHANGE-IN-PRODUCTION"
    )
    FLASK_ENV: str = os.environ.get("FLASK_ENV", "development")
    DEBUG: bool = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    # ------------------------------------------------------------------
    # File upload / output
    # ------------------------------------------------------------------
    UPLOAD_FOLDER: str = os.environ.get(
        "UPLOAD_FOLDER", str(BASE_DIR / "uploads")
    )
    OUTPUT_FOLDER: str = os.environ.get(
        "OUTPUT_FOLDER", str(BASE_DIR / "outputs")
    )
    MAX_CONTENT_LENGTH: int = 50 * 1024 * 1024  # 50 MB hard limit
    ALLOWED_EXTENSIONS: frozenset = frozenset({"csv", "xlsx", "pdf"})

    # ------------------------------------------------------------------
    # Reference data paths (can be overridden for SharePoint / Azure SQL)
    # ------------------------------------------------------------------
    PROPERTY_MASTER_PATH: str = os.environ.get(
        "PROPERTY_MASTER_PATH",
        str(BASE_DIR / "data" / "property_master.csv"),
    )
    RATE_MAPPING_PATH: str = os.environ.get(
        "RATE_MAPPING_PATH",
        str(BASE_DIR / "data" / "rate_mapping.csv"),
    )
    PROPERTY_ALIASES_PATH: str = os.environ.get(
        "PROPERTY_ALIASES_PATH",
        str(BASE_DIR / "data" / "property_aliases.csv"),
    )
    PROPERTY_ROLLUPS_PATH: str = os.environ.get(
        "PROPERTY_ROLLUPS_PATH",
        str(BASE_DIR / "config" / "property_mappings" / "boom_property_rollups.csv"),
    )

    # ------------------------------------------------------------------
    # SQLite database
    # ------------------------------------------------------------------
    DATABASE_PATH: str = os.environ.get(
        "DATABASE_PATH",
        str(BASE_DIR / "reconciliation.db"),
    )

    # ------------------------------------------------------------------
    # Reconciliation tolerances (stored as strings, converted at use)
    # ------------------------------------------------------------------
    INVOICE_TOLERANCE: Decimal = Decimal(
        os.environ.get("INVOICE_TOLERANCE", "0.01")
    )
    BALANCE_TOLERANCE: Decimal = Decimal(
        os.environ.get("BALANCE_TOLERANCE", "0.05")
    )

    # ------------------------------------------------------------------
    # Session / cookie security
    # ------------------------------------------------------------------
    SESSION_COOKIE_SECURE: bool = (
        os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    )
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"

    # ------------------------------------------------------------------
    # Supported vendors
    # ------------------------------------------------------------------
    SUPPORTED_VENDORS: list = [
        "Rent Plus",
        "Credit Boost powered by Boom",
    ]

    # ------------------------------------------------------------------
    # Fuzzy-match threshold (0-100).  Scores below this are not suggested.
    # ------------------------------------------------------------------
    FUZZY_MATCH_THRESHOLD: int = int(
        os.environ.get("FUZZY_MATCH_THRESHOLD", "80")
    )


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True


_config_map: dict = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}


def get_config() -> type:
    """Return the active Config class based on FLASK_ENV."""
    env = os.environ.get("FLASK_ENV", "development")
    return _config_map.get(env, DevelopmentConfig)
