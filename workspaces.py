"""Persistent workspace management for the IndieK API."""

from __future__ import annotations

import json
import logging
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile as StarletteUploadFile

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(__file__).resolve().parent / ".workspaces"
WORKSPACE_ROOT.mkdir(exist_ok=True)
workspaces: dict[str, dict[str, Any]] = {}
archive_hash_index: dict[str, str] = {}


def iso_timestamp() -> str:
    """Return a compact UTC timestamp for workspace metadata."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def workspace_dir(network_id: str | None = None) -> Path:
    """Return the on-disk directory for a workspace."""
    if not network_id:
        raise HTTPException(status_code=400, detail="network_id is required")
    return WORKSPACE_ROOT / network_id


def save_workspace_meta(workspace: dict[str, Any]) -> None:
    """Persist workspace metadata alongside its archive."""
    path = workspace_dir(workspace["network_id"]) / "workspace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(workspace, indent=2), encoding="utf-8")


def _load_workspace_meta(directory: Path) -> dict[str, Any] | None:
    path = directory / "workspace.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load workspace metadata from %s: %s", path, exc)
        return None


def load_all_workspaces() -> None:
    """Load persisted workspace records and their duplicate-detection index."""
    for child in WORKSPACE_ROOT.iterdir():
        if not child.is_dir() or child.name in workspaces:
            continue
        record = _load_workspace_meta(child) or {
            "network_id": child.name,
            "source_type": "archive",
            "created_at": iso_timestamp(),
            "source_url": None,
            "uploaded_file": None,
            "filename": None,
            "last_event_hash": None,
        }
        workspaces[child.name] = record
        if last_hash := record.get("last_event_hash"):
            archive_hash_index[last_hash] = child.name
    logger.info("Loaded %d workspace(s)", len(workspaces))


def get_workspace(network_id: str | None = None) -> dict[str, Any]:
    """Return an existing workspace."""
    if not network_id or network_id not in workspaces:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspaces[network_id]


def _safe_extract_zip(archive: zipfile.ZipFile, destination: Path) -> None:
    """Extract an archive while rejecting traversal paths."""
    destination = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if destination not in target.parents and target != destination:
            raise HTTPException(
                status_code=400, detail="ZIP archive contains an unsafe path"
            )
    archive.extractall(destination)


def resolve_archive_root(network_id: str | None = None) -> Path:
    """Resolve a workspace to an archive root, extracting uploads once."""
    workspace = get_workspace(network_id)
    if archive_root := workspace.get("archive_root"):
        return Path(archive_root).expanduser().resolve()

    directory = workspace_dir(workspace["network_id"])
    directory.mkdir(parents=True, exist_ok=True)
    uploaded_file = workspace.get("uploaded_file")
    if uploaded_file:
        uploaded_path = Path(uploaded_file)
        if uploaded_path.is_file() and uploaded_path.suffix.lower() == ".zip":
            expected_root = directory / uploaded_path.stem
            if not expected_root.exists():
                with zipfile.ZipFile(uploaded_path, "r") as archive:
                    _safe_extract_zip(archive, directory)
            archive_root = expected_root if expected_root.exists() else directory
        else:
            archive_root = directory
    else:
        archive_root = directory

    workspace["archive_root"] = str(archive_root.resolve())
    save_workspace_meta(workspace)
    return archive_root.resolve()


async def parse_workspace_payload(
    request: Request,
) -> tuple[str, str | None, StarletteUploadFile | None]:
    """Read either JSON or multipart workspace creation input."""
    if "multipart/form-data" in request.headers.get("content-type", ""):
        form = await request.form()
        file = form.get("file")
        return (
            str(form.get("sourceType") or "upload"),
            str(form.get("url") or "") or None,
            file if isinstance(file, StarletteUploadFile) else None,
        )
    try:
        payload = await request.json()
    except ValueError, json.JSONDecodeError:
        payload = {}
    url = payload.get("url")
    return (
        str(payload.get("sourceType") or "url"),
        url if isinstance(url, str) else None,
        None,
    )


async def create_workspace(request: Request) -> dict[str, Any]:
    """Create a workspace record and store an optional uploaded ZIP."""
    source_type, url, uploaded_file = await parse_workspace_payload(request)
    if source_type == "url" and not url:
        raise HTTPException(status_code=400, detail="url is required")
    if source_type == "upload" and uploaded_file is None:
        raise HTTPException(status_code=400, detail="file is required")
    if source_type not in {"url", "upload"}:
        raise HTTPException(
            status_code=400, detail="sourceType must be 'url' or 'upload'"
        )

    network_id = str(uuid.uuid4())
    directory = workspace_dir(network_id)
    directory.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "network_id": network_id,
        "source_type": source_type,
        "created_at": iso_timestamp(),
        "source_url": url,
        "uploaded_file": None,
        "filename": None,
        "last_event_hash": None,
    }
    if uploaded_file is not None:
        filename = Path(uploaded_file.filename or "workspace-upload.zip").name
        destination = directory / filename
        with destination.open("wb") as buffer:
            shutil.copyfileobj(uploaded_file.file, buffer)
        await uploaded_file.close()
        record["uploaded_file"] = str(destination)
        record["filename"] = filename
    workspaces[network_id] = record
    save_workspace_meta(record)
    return record


def record_archive_head(network_id: str, last_event_hash: str | None) -> None:
    """Store an archive head and prevent duplicate workspace registrations."""
    if not last_event_hash:
        return
    existing = archive_hash_index.get(last_event_hash)
    if existing and existing != network_id:
        raise HTTPException(
            status_code=409,
            detail=f"This archive is already cached as workspace '{existing}'.",
            headers={"X-Existing-Network-Id": existing},
        )
    workspace = get_workspace(network_id)
    workspace["last_event_hash"] = last_event_hash
    archive_hash_index[last_event_hash] = network_id
    save_workspace_meta(workspace)


def list_workspace_summaries() -> list[dict[str, Any]]:
    """Return browser-safe workspace summary records."""
    return [
        {
            "network_id": item["network_id"],
            "source_type": item.get("source_type"),
            "created_at": item.get("created_at"),
            "filename": item.get("filename"),
            "source_url": item.get("source_url"),
        }
        for item in workspaces.values()
    ]
