"""Domain errors raised by the API and their mapping to HTTP responses.

Every error response has the shape ``{"detail": <message>, "code": <stable slug>}``
so clients can branch on ``code`` without parsing library messages.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from indiek import (
    ArchiveLockedError,
    AuthorshipError,
    IndieKError,
    IntegrityError,
    UnknownItemError,
)


class IndieKAPIError(Exception):
    """Base class for errors that map to a deliberate HTTP response."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail

    def extra(self) -> dict[str, Any]:
        """Additional machine-readable fields of the error response."""
        return {}


class ArchiveNotFoundError(IndieKAPIError):
    status_code, code = 404, "archive_not_found"


class WorkspaceNotFoundError(IndieKAPIError):
    status_code, code = 404, "workspace_not_found"


class ItemNotFoundError(IndieKAPIError):
    status_code, code = 404, "item_not_found"


class BlobNotFoundError(IndieKAPIError):
    status_code, code = 404, "blob_not_found"


class ForbiddenError(IndieKAPIError):
    status_code, code = 403, "forbidden"


class ArchiveIntegrityError(IndieKAPIError):
    """The archive failed verification: broken chain, bad signature or missing blob."""

    status_code, code = 409, "archive_integrity"


class ArchiveLockedAPIError(IndieKAPIError):
    """Another process (e.g. the `ik` CLI) kept the archive's write lock too long; retry."""

    status_code, code = 409, "archive_locked"


class WorkspaceConflictError(IndieKAPIError):
    status_code, code = 409, "workspace_conflict"


class FolderNotEmptyError(IndieKAPIError):
    """A new archive can only be created in an empty folder (hidden entries aside)."""

    status_code, code = 409, "folder_not_empty"


class AmbiguousArchiveError(IndieKAPIError):
    """The folder is not an archive but holds several archive subfolders."""

    status_code, code = 409, "ambiguous_archive"

    def __init__(self, detail: str, candidates: list[str]) -> None:
        super().__init__(detail)
        self.candidates = candidates

    def extra(self) -> dict[str, Any]:
        return {"candidates": self.candidates}


class NotAnArchiveError(IndieKAPIError):
    """Neither the folder nor any of its direct subfolders is an IndieK archive."""

    status_code, code = 422, "not_an_archive"


class ArchiveRuleError(IndieKAPIError):
    """The request violates an archive rule (e.g. a node tag applied to an edge)."""

    status_code, code = 422, "archive_rule"


class MissingAuthorKeyError(IndieKAPIError):
    status_code, code = 503, "author_key_missing"


def translate_library_error(exc: Exception) -> IndieKAPIError:
    """Classify an exception raised by the `indiek` SDK by its type."""
    if isinstance(exc, IndieKAPIError):
        return exc
    message = str(exc) or type(exc).__name__
    if isinstance(exc, IntegrityError):
        return ArchiveIntegrityError(message)
    if isinstance(exc, ArchiveLockedError):
        return ArchiveLockedAPIError(message)
    if isinstance(exc, AuthorshipError):
        return ForbiddenError(message)
    if isinstance(exc, UnknownItemError):
        return ItemNotFoundError(message)
    # The SDK's ArchiveRuleError, and ValueError from its input validation.
    return ArchiveRuleError(message)


@contextmanager
def library_errors() -> Iterator[None]:
    """Translate expected `indiek` failures; anything else propagates as a bug (500)."""
    try:
        yield
    except IndieKAPIError:
        raise
    except (IndieKError, ValueError) as exc:
        raise translate_library_error(exc) from exc


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(IndieKAPIError)
    async def _handle(_: Request, exc: IndieKAPIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": exc.code, **exc.extra()},
        )
