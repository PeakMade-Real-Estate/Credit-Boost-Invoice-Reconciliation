"""
Property state lookup via the shared "Properties_0" SharePoint list.

Fetches ``PROPERTY_NAME`` / ``ADDRESS_STATE`` for every property from the
SharePoint list (Microsoft Graph) and exposes a small, in-process cached API
so callers can check whether a given property is located in California.
Used to apply the California-specific Credit Boost rate instead of the
standard flat rate.

This never raises for the caller — any Graph/auth failure is logged and the
last known cache is returned (empty if there is none yet), so a down or
misconfigured SharePoint connection never blocks reconciliation.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from services.utils import normalize_text

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_CACHE_TTL_SECONDS = 900  # 15 minutes

_cache: Dict[str, str] = {}
_cache_fetched_at: Optional[float] = None


def get_property_states(force_refresh: bool = False) -> Dict[str, str]:
    """Return ``{normalized_property_name: two-letter state code}``.

    Best-effort: on any failure, logs a warning and returns the last known
    cache (possibly empty) rather than raising.
    """
    global _cache, _cache_fetched_at

    if (
        not force_refresh
        and _cache_fetched_at is not None
        and (time.monotonic() - _cache_fetched_at) < _CACHE_TTL_SECONDS
    ):
        return _cache

    try:
        _cache = _fetch_property_states()
        _cache_fetched_at = time.monotonic()
    except Exception as exc:
        logger.warning(
            "Could not refresh property states from SharePoint; using %s cache: %s",
            "stale" if _cache else "empty",
            exc,
        )

    return _cache


def is_california(
    property_name: str, property_states: Optional[Dict[str, str]] = None
) -> bool:
    """True if ``property_name`` resolves to a California (CA) property."""
    states = property_states if property_states is not None else get_property_states()
    return states.get(normalize_text(property_name)) == "CA"


def _fetch_property_states() -> Dict[str, str]:
    """Pull PROPERTY_NAME/ADDRESS_STATE from the Properties SharePoint list."""
    import requests

    from config import Config
    from services.sharepoint_service import SharePointConfigError, _acquire_token

    cfg = {
        "client_id": Config.AZURE_CLIENT_ID,
        "tenant_id": Config.AZURE_TENANT_ID,
        "client_secret": Config.AZURE_CLIENT_SECRET,
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise SharePointConfigError(
            f"Missing required Azure configuration: {', '.join(missing)}"
        )
    if not Config.SHAREPOINT_HOSTNAME:
        raise SharePointConfigError(
            "Missing required configuration: SHAREPOINT_HOSTNAME"
        )

    token = _acquire_token(cfg)
    headers = {"Authorization": f"Bearer {token}"}

    site_url = (
        f"{_GRAPH_BASE}/sites/{Config.SHAREPOINT_HOSTNAME}:"
        f"{Config.SHAREPOINT_PROPERTIES_SITE_PATH}"
    )
    site_resp = requests.get(site_url, headers=headers, timeout=30)
    site_resp.raise_for_status()
    site_id = site_resp.json()["id"]

    states: Dict[str, str] = {}
    url = (
        f"{_GRAPH_BASE}/sites/{site_id}/lists/{Config.SHAREPOINT_PROPERTIES_LIST_ID}/items"
        f"?expand=fields(select=PROPERTY_NAME,ADDRESS_STATE)&$top=200"
    )
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("value", []):
            fields = item.get("fields", {})
            name = fields.get("PROPERTY_NAME")
            state = fields.get("ADDRESS_STATE")
            if name and state:
                states[normalize_text(name)] = state
        url = data.get("@odata.nextLink")

    logger.info("Loaded %d property states from SharePoint.", len(states))
    return states
