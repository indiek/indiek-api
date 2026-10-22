"""Registry of local archive folders opened in the desktop app.

A workspace is only a pointer to a folder (typically a git repo): archives are
read and written in place, never copied. Removing a workspace never touches files.

A folder is an archive when it holds ``MANIFEST.json``, ``events/`` or
``meta/events/``. Registering a folder that is not an archive opens its only
archive subfolder (e.g. a repo with the archive in ``kg/``); it never writes files
unless a new archive is explicitly requested in an empty or missing folder.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from indiek import Archive, Author

from indiek_api.archive_service import ArchiveService
from indiek_api.errors import (
    AmbiguousArchiveError,
    ArchiveNotFoundError,
    FolderNotEmptyError,
    NotAnArchiveError,
    WorkspaceNotFoundError,
    library_errors,
)

logger = logging.getLogger(__name__)

_ARCHIVE_MARKERS = ("MANIFEST.json", "events", "meta/events")


def is_archive(folder: Path) -> bool:
    return any((folder / marker).exists() for marker in _ARCHIVE_MARKERS)


def _existing_folder(path: str | Path) -> Path:
    folder = Path(path).expanduser().resolve()
    if not folder.is_dir():
        raise ArchiveNotFoundError(f"Archive folder does not exist: {folder}")
    return folder


def _visible_entries(folder: Path) -> list[Path]:
    """Entries of ``folder`` except hidden ones (``.git``, ``.DS_Store``…)."""
    return sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))


def locate_archive(path: str | Path) -> Path:
    """The archive at ``path``, or the single archive among its direct subfolders."""
    folder = _existing_folder(path)
    if is_archive(folder):
        return folder
    try:
        candidates = [e for e in _visible_entries(folder) if e.is_dir() and is_archive(e)]
    except OSError as exc:
        raise NotAnArchiveError(f"Cannot list {folder}: {exc}") from exc
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AmbiguousArchiveError(
            f"{folder} holds several IndieK archives; choose one.",
            [str(candidate) for candidate in candidates],
        )
    raise NotAnArchiveError(
        f"No IndieK archive in {folder} or its subfolders (no MANIFEST.json or events/)."
    )


def create_archive(path: str | Path, author: Author | None) -> Path:
    """Initialise an archive (MANIFEST.json, SPEC.md) in an empty folder.

    A missing folder is created when its parent exists (like ``git init <dir>``);
    a missing parent is refused so that a typo does not create a folder tree.
    """
    folder = Path(path).expanduser().resolve()
    if not folder.exists():
        if not folder.parent.is_dir():
            raise ArchiveNotFoundError(f"Parent folder does not exist: {folder.parent}")
        folder.mkdir()
    folder = _existing_folder(folder)
    if _visible_entries(folder):
        raise FolderNotEmptyError(
            f"A new archive needs an empty folder (hidden entries such as .git aside): {folder}"
        )
    with library_errors():
        Archive.open(folder, author=author).write_manifest()
    return folder


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    name: str
    path: str
    created_at: str


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class WorkspaceRegistry:
    def __init__(self, registry_path: Path) -> None:
        self._path = Path(registry_path).expanduser()
        self._lock = threading.RLock()
        self._records: dict[str, WorkspaceRecord] = {}
        self._services: dict[str, ArchiveService] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            records = [WorkspaceRecord(**item) for item in raw["workspaces"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            backup = self._path.with_name(
                f"{self._path.name}.corrupt-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
            )
            self._path.replace(backup)
            logger.warning("Unreadable workspace registry moved to %s: %s", backup, exc)
            return
        self._records = {record.workspace_id: record for record in records}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workspaces": [asdict(record) for record in self._records.values()]}
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def list(self) -> list[WorkspaceRecord]:
        with self._lock:
            return list(self._records.values())

    def get(self, workspace_id: str) -> WorkspaceRecord:
        with self._lock:
            try:
                return self._records[workspace_id]
            except KeyError:
                raise WorkspaceNotFoundError(f"Unknown workspace '{workspace_id}'") from None

    def register(
        self, path: str | Path, *, create: bool = False, author: Author | None = None
    ) -> tuple[WorkspaceRecord, bool]:
        """Register an archive folder after verifying it; return ``(record, created)``.

        With ``create``, first initialise a new archive in the empty folder ``path``
        (``author`` is listed in its MANIFEST.json); otherwise locate the archive at
        or directly under ``path``.
        """
        with self._lock:
            root = create_archive(path, author) if create else locate_archive(path)
            service = ArchiveService(root)
            resolved = str(service.root)
            for record in self._records.values():
                if record.path == resolved:
                    return record, False
            service.projection()  # raises ArchiveIntegrityError before anything is saved
            record = WorkspaceRecord(
                workspace_id=str(uuid.uuid4()),
                name=service.root.name,
                path=resolved,
                created_at=_utc_now(),
            )
            self._records[record.workspace_id] = record
            self._services[record.workspace_id] = service
            self._save()
            return record, True

    def remove(self, workspace_id: str) -> None:
        with self._lock:
            self.get(workspace_id)
            del self._records[workspace_id]
            self._services.pop(workspace_id, None)
            self._save()

    def service(self, workspace_id: str) -> ArchiveService:
        """The shared service of a workspace; its lock serialises every write to the folder."""
        with self._lock:
            record = self.get(workspace_id)
            if workspace_id not in self._services:
                self._services[workspace_id] = ArchiveService(Path(record.path))
            return self._services[workspace_id]
