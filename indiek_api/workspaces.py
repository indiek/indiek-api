"""Registry of local archive folders opened in the desktop app.

A workspace is only a pointer to a folder (typically a git repo): archives are
read and written in place, never copied. Removing a workspace never touches files.
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

from indiek_api.archive_service import ArchiveService
from indiek_api.errors import WorkspaceNotFoundError

logger = logging.getLogger(__name__)


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

    def register(self, path: str | Path) -> tuple[WorkspaceRecord, bool]:
        """Register a folder after verifying it; return ``(record, created)``."""
        service = ArchiveService(Path(path))  # raises ArchiveNotFoundError
        resolved = str(service.root)
        with self._lock:
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
