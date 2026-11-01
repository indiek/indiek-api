"""Reuse seams of the API (DESKTOP:REUSE-SEAMS, owner decisions 2026-10-30).

`create_app(..., workspaces=..., signer=...)` accepts any object with these methods, so
another host can embed the API as a library with its own workspace storage or signing,
without forking it. Defaults: `WorkspaceRegistry` (local folders) and `KeyProvider`
(`IK_AUTHOR_PRIVATE_KEY` or the OS keychain).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from indiek import Author, keys

from indiek_api.archive_service import ArchiveService
from indiek_api.workspaces import WorkspaceRecord


@runtime_checkable
class WorkspaceProvider(Protocol):
    """Where the archives served by the API live (the routes use only these methods)."""

    def list(self) -> list[WorkspaceRecord]: ...

    def register(
        self, path: str | Path, *, create: bool = False, author: Author | None = None
    ) -> tuple[WorkspaceRecord, bool]: ...

    def remove(self, workspace_id: str) -> None: ...

    def service(self, workspace_id: str) -> ArchiveService: ...


@runtime_checkable
class Signer(Protocol):
    """Who signs archive writes."""

    def author(self) -> Author:
        """The signing author; raises `MissingAuthorKeyError` when there is none."""
        ...

    def author_or_none(self) -> Author | None: ...


@runtime_checkable
class KeyManager(Signer, Protocol):
    """A signer whose key the user manages through the `/api/identity/*` routes.

    These routes are mounted only when the signer implements this protocol.
    """

    def status(self) -> tuple[Author | None, keys.KeySource, bool]: ...

    def store(self, author: Author, *, replace: bool) -> None: ...

    def backup(self, password: str) -> tuple[Author, bytes]: ...

    def read_backup(self, data: str, password: str) -> Author: ...
