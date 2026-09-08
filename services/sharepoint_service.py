"""
SharePoint document library connector (Microsoft Graph).

Archives reconciliation run documents (uploaded source files + generated
output workbooks) to a SharePoint document library, one folder per run ID.

Authentication uses the MSAL confidential client (app-only / client
credentials) flow with the ``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` /
``AZURE_CLIENT_SECRET`` values already used for Azure AD.  The app
registration must be granted a Microsoft Graph *application* permission of
either ``Sites.Selected`` (scoped to just this site) or
``Sites.ReadWrite.All``, with admin consent.  If using ``Sites.Selected``, a
site administrator must additionally grant this specific app write access to
the target site via a one-time Graph permissions call.

Usage::

    from services.sharepoint_service import verify_connection

    ok, details = verify_connection()
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_token_cache: dict = {}

# Maps internal enum/model values to the exact SharePoint choice-column labels.
_STATUS_LABELS = {
    "passed": "Passed",
    "passed_with_warnings": "Passed with Warnings",
    "requires_review": "Requires Review",
    "failed": "Failed",
}
_RUN_TYPE_LABELS = {
    "invoice_cash": "Invoice vs Cash",
    "resident_charges": "Resident Charges",
}


class SharePointConfigError(Exception):
    """Raised when required SharePoint/Graph configuration is missing."""


class SharePointConnectionError(Exception):
    """Raised when a Graph API call fails."""


def _get_config() -> dict:
    from config import Config

    cfg = {
        "client_id": Config.AZURE_CLIENT_ID,
        "tenant_id": Config.AZURE_TENANT_ID,
        "client_secret": Config.AZURE_CLIENT_SECRET,
        "hostname": Config.SHAREPOINT_HOSTNAME,
        "site_path": Config.SHAREPOINT_SITE_PATH,
        "library_id": Config.SHAREPOINT_LIBRARY_ID,
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise SharePointConfigError(
            f"Missing required SharePoint configuration: {', '.join(missing)}"
        )
    return cfg


def _acquire_token(cfg: dict) -> str:
    """Acquire (and cache) a Graph app-only access token via MSAL."""
    import msal

    app = msal.ConfidentialClientApplication(
        client_id=cfg["client_id"],
        client_credential=cfg["client_secret"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
    )
    result = app.acquire_token_silent(
        scopes=["https://graph.microsoft.com/.default"], account=None
    )
    if not result:
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
    if "access_token" not in result:
        raise SharePointConnectionError(
            f"Failed to acquire Graph token: {result.get('error')} - "
            f"{result.get('error_description')}"
        )
    return result["access_token"]


def _graph_get(url: str, token: str) -> dict:
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    if resp.status_code != 200:
        raise SharePointConnectionError(
            f"Graph GET {url} failed: {resp.status_code} {resp.text}"
        )
    return resp.json()


def get_site_id(token: str, hostname: str, site_path: str) -> str:
    """Resolve the Graph site ID from a hostname + site-relative path."""
    url = f"{_GRAPH_BASE}/sites/{hostname}:{site_path}"
    data = _graph_get(url, token)
    return data["id"]


def get_drive_id(token: str, site_id: str, library_id: str) -> str:
    """Resolve the Drive ID backing a document library (List ID)."""
    url = f"{_GRAPH_BASE}/sites/{site_id}/lists/{library_id}/drive"
    data = _graph_get(url, token)
    return data["id"]


def _auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _get_or_create_run_folder(token: str, drive_id: str, run_id: str) -> str:
    """Return the folder item ID for ``run_id``, creating it if needed."""
    get_url = f"{_GRAPH_BASE}/drives/{drive_id}/root:/{run_id}"
    resp = requests.get(get_url, headers=_auth_header(token), timeout=30)
    if resp.status_code == 200:
        return resp.json()["id"]
    if resp.status_code != 404:
        raise SharePointConnectionError(
            f"Graph GET {get_url} failed: {resp.status_code} {resp.text}"
        )

    create_url = f"{_GRAPH_BASE}/drives/{drive_id}/root/children"
    body = {
        "name": run_id,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    resp = requests.post(
        create_url,
        headers={**_auth_header(token), "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise SharePointConnectionError(
            f"Graph POST {create_url} failed: {resp.status_code} {resp.text}"
        )
    return resp.json()["id"]


def _upload_file(
    token: str, drive_id: str, folder_id: str, local_path: str, remote_filename: str
) -> str:
    """Upload a local file into a folder (simple upload; fine for our file sizes)."""
    url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{folder_id}:/{remote_filename}:/content"
    with open(local_path, "rb") as fh:
        content = fh.read()
    resp = requests.put(
        url,
        headers={**_auth_header(token), "Content-Type": "application/octet-stream"},
        data=content,
        timeout=120,
    )
    if resp.status_code not in (200, 201):
        raise SharePointConnectionError(
            f"Graph PUT {url} failed: {resp.status_code} {resp.text}"
        )
    return resp.json()["id"]


def _set_item_fields(token: str, drive_id: str, item_id: str, fields: dict) -> None:
    url = f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}/listItem/fields"
    resp = requests.patch(
        url,
        headers={**_auth_header(token), "Content-Type": "application/json"},
        json=fields,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise SharePointConnectionError(
            f"Graph PATCH {url} failed: {resp.status_code} {resp.text}"
        )


def archive_run(run_meta, documents: Dict[str, Optional[str]]) -> dict:
    """Archive a reconciliation run's documents to the SharePoint library.

    Creates (or reuses) a folder named after ``run_meta.run_id``, uploads
    each provided document, and sets metadata columns on the folder (run
    summary) and on each file (``RunID`` + ``DocumentType``).

    Args:
        run_meta: A ``ReconciliationRun`` instance.
        documents: Mapping of Document Type label (must match the
            ``DocumentType`` choice column, e.g. ``"Vendor Invoice"``) to a
            local file path.  Falsy/missing paths are skipped.

    Returns:
        Dict with ``folder_id`` and ``uploaded`` (Document Type -> item ID).
    """
    cfg = _get_config()
    token = _acquire_token(cfg)
    site_id = get_site_id(token, cfg["hostname"], cfg["site_path"])
    drive_id = get_drive_id(token, site_id, cfg["library_id"])

    folder_id = _get_or_create_run_folder(token, drive_id, run_meta.run_id)

    status_value = getattr(run_meta.status, "value", run_meta.status)
    folder_fields = {
        "RunID": run_meta.run_id,
        "Vendor": run_meta.vendor,
        "RunType": _RUN_TYPE_LABELS.get(run_meta.run_type, run_meta.run_type),
        "ReportingMonth": run_meta.reporting_month,
        "Status": _STATUS_LABELS.get(status_value, str(status_value)),
        "ExceptionCount": run_meta.exception_count,
        "BlockingExceptionCount": run_meta.blocking_exception_count,
        "UploadedBy": run_meta.uploaded_by,
        "UploadedDate": run_meta.uploaded_date.isoformat(),
    }
    _set_item_fields(token, drive_id, folder_id, folder_fields)

    uploaded: Dict[str, str] = {}
    for doc_type, path in documents.items():
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            logger.warning("Skipping SharePoint upload; file not found: %s", path)
            continue
        item_id = _upload_file(token, drive_id, folder_id, str(p), p.name)
        _set_item_fields(
            token, drive_id, item_id, {"RunID": run_meta.run_id, "DocumentType": doc_type}
        )
        uploaded[doc_type] = item_id

    logger.info(
        "Archived run %s to SharePoint: folder=%s files=%d",
        run_meta.run_id, folder_id, len(uploaded),
    )
    return {"folder_id": folder_id, "uploaded": uploaded}


def verify_connection() -> Tuple[bool, dict]:
    """Run an end-to-end connectivity check without uploading anything.

    Returns:
        (success, details) where ``details`` contains resolved IDs on
        success, or an ``error`` message on failure.
    """
    try:
        cfg = _get_config()
        token = _acquire_token(cfg)
        site_id = get_site_id(token, cfg["hostname"], cfg["site_path"])
        drive_id = get_drive_id(token, site_id, cfg["library_id"])
        details = {
            "site_id": site_id,
            "drive_id": drive_id,
            "library_id": cfg["library_id"],
        }
        logger.info("SharePoint connection verified: %s", details)
        return True, details
    except (SharePointConfigError, SharePointConnectionError) as exc:
        logger.error("SharePoint connection check failed: %s", exc)
        return False, {"error": str(exc)}
    except Exception as exc:  # pragma: no cover - unexpected failure
        logger.error("SharePoint connection check failed unexpectedly: %s", exc)
        return False, {"error": f"Unexpected error: {exc}"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok, info = verify_connection()
    if ok:
        print("SUCCESS")
        for k, v in info.items():
            print(f"  {k} = {v}")
    else:
        print("FAILED")
        print(f"  {info.get('error')}")
