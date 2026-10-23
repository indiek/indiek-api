"""Verified, cached and write-serialised access to one IndieK archive folder.

The archive is re-verified (chain, signatures, blobs) whenever its event logs
change on disk, whoever changed them: this API, the ``ik`` CLI or a ``git pull``.

Known limits:
- A blob altered on disk *without* any log change is only caught by the next full
  verification, i.e. after the next log change.
- The write lock serialises writers inside this process only; it does not guard
  against the ``ik`` CLI appending to the same archive at the same instant.

Verification uses the SDK's default ``incremental`` level: hash chains are always
recomputed; signatures and blobs already vouched for by this machine's
verification cache (``IK_CACHE_DIR``, outside the archive) are not re-checked.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from indiek import Archive, Author, ItemKind

from indiek_api.errors import ArchiveNotFoundError, library_errors

_DIGEST = re.compile(r"[0-9a-f]{64}")
_LOG_DIRS = ("events", "meta/events")
_NETWORKS: tuple[Literal["base", "meta"], ...] = ("base", "meta")
_TAG_KINDS: dict[ItemKind, Literal["node", "edge"]] = {
    ItemKind.NODE_TAG: "node",
    ItemKind.EDGE_TAG: "edge",
}
_MAX_EDGE_TEXT = 64 * 1024  # edges are short plain-text labels; larger blobs are not inlined

type Fingerprint = tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class NodeView:
    id: str
    alias: str
    title: str | None
    author: str
    resource_type: str
    media_type: str | None
    blob_hash: str | None
    uri: str | None
    path: str | None
    file_size: int | None
    file_hash: str | None
    last_verified: str | None
    tags: tuple[str, ...]
    revised: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EdgeView:
    id: str
    alias: str
    source: str
    target: str
    title: str | None
    author: str
    resource_type: str  # "managed" or "none" (content-less, indiek format 3.1)
    media_type: str | None
    blob_hash: str | None
    tags: tuple[str, ...]
    revised: bool
    created_at: str
    updated_at: str
    content: str | None


@dataclass(frozen=True, slots=True)
class TagView:
    id: str
    alias: str
    name: str
    description: str
    kind: Literal["node", "edge"]
    author: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EventView:
    """One line of the base or meta event log, in log order."""

    network: Literal["base", "meta"]
    timestamp: str
    author: str
    event_type: str
    item_id: str | None
    alias: str | None


@dataclass(frozen=True, slots=True)
class Projection:
    """Current state of an archive: supersedes applied, bookkeeping meta items hidden."""

    head: str | None
    meta_head: str | None
    nodes: dict[str, NodeView]
    edges: dict[str, EdgeView]
    tags: dict[str, TagView]
    blob_media_types: dict[str, str]


def _digest(blob_hash: str) -> str | None:
    digest = blob_hash.removeprefix("sha256:")
    return digest if _DIGEST.fullmatch(digest) else None


def _aliases(archive: Archive) -> dict[str, str]:
    return {str(uid): item.alias for uid, item in archive.index.items.items()}


def _read_events(archive: Archive, aliases: dict[str, str]) -> list[EventView]:
    """Base then meta event log, each in append order (what ``ik read`` prints)."""
    events: list[EventView] = []
    for network in _NETWORKS:
        for event in archive.events(network):
            header, body = event["header"], event["body"]
            item = body.get("edge_id") or body.get("node_id")
            item_id = str(item) if item else None
            events.append(
                EventView(
                    network=network,
                    timestamp=header["timestamp"],
                    author=header["author"],
                    event_type=body["event_type"],
                    item_id=item_id,
                    alias=aliases.get(item_id) if item_id else None,
                )
            )
    return events


def _edge_text(blob: Path | None, media_type: str | None) -> str | None:
    if blob is None or not (media_type or "").startswith("text/plain"):
        return None
    if not blob.is_file() or blob.stat().st_size > _MAX_EDGE_TEXT:
        return None
    return blob.read_text(encoding="utf-8", errors="replace")


def _project(archive: Archive, events: list[EventView], aliases: dict[str, str]) -> Projection:
    times: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.item_id:
            created = times.get(event.item_id, (event.timestamp,))[0]
            times[event.item_id] = (created, event.timestamp)
    blobs = archive.index.store.blobs
    nodes: dict[str, NodeView] = {}
    edges: dict[str, EdgeView] = {}
    tags: dict[str, TagView] = {}
    media_types: dict[str, str] = {}

    for uid, item in archive.index.items.items():
        item_id, body = str(uid), item.body
        alias = aliases.get(item_id, "")
        created_at, updated_at = times.get(item_id, ("", ""))
        if item.is_meta:
            if tag_kind := _TAG_KINDS.get(item.kind):
                tags[item_id] = TagView(
                    id=item_id,
                    alias=alias,
                    name=body.get("name", ""),
                    description=body.get("description", ""),
                    kind=tag_kind,
                    author=item.author,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            continue

        common: dict[str, Any] = {
            "id": item_id,
            "alias": alias,
            "title": body.get("title"),
            "author": item.author,
            "resource_type": body.get("resource_type") or "managed",
            "media_type": body.get("media_type"),
            "blob_hash": body.get("blob_hash"),
            "tags": tuple(sorted(str(tag) for tag in item.tags)),
            "revised": str(body.get("event_type", "")).startswith("supersede_"),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        digest = _digest(common["blob_hash"] or "")
        if digest and common["media_type"]:
            media_types[digest] = common["media_type"]

        if item.kind is ItemKind.EDGE:
            edges[item_id] = EdgeView(
                source=str(body["source_node"]),
                target=str(body["target_node"]),
                content=_edge_text(
                    blobs.path_for(digest) if digest else None, common["media_type"]
                ),
                **common,
            )
        else:
            nodes[item_id] = NodeView(
                uri=body.get("uri"),
                path=body.get("path"),
                file_size=body.get("file_size"),
                file_hash=body.get("file_hash"),
                last_verified=body.get("last_verified"),
                **common,
            )

    return Projection(
        head=archive.head,
        meta_head=archive.meta_head,
        nodes=nodes,
        edges=edges,
        tags=tags,
        blob_media_types=media_types,
    )


class ArchiveService:
    """One instance per archive folder; share it so the write lock is meaningful."""

    def __init__(self, root: Path) -> None:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ArchiveNotFoundError(f"Archive folder does not exist: {root}")
        self.root = root
        self._lock = threading.RLock()
        self._cache: tuple[Fingerprint, Archive, Projection, list[EventView]] | None = None

    def _fingerprint(self) -> Fingerprint:
        entries = []
        for log_dir in _LOG_DIRS:
            for path in (self.root / log_dir).rglob("*ndjson"):
                stat = path.stat()
                entries.append((str(path), stat.st_size, stat.st_mtime_ns))
        return tuple(sorted(entries))

    def _open(self, author: Author | None = None) -> Archive:
        """Open and verify the archive; raises ArchiveIntegrityError on tampering."""
        with library_errors():
            return Archive.open(self.root, author=author)

    def _load(self) -> tuple[Archive, Projection, list[EventView]]:
        with self._lock:
            fingerprint = self._fingerprint()
            if self._cache is None or self._cache[0] != fingerprint:
                archive = self._open()
                aliases = _aliases(archive)
                events = _read_events(archive, aliases)
                self._cache = (fingerprint, archive, _project(archive, events, aliases), events)
            return self._cache[1], self._cache[2], self._cache[3]

    def archive(self) -> Archive:
        """The cached read-only archive. Mutate through :meth:`write`."""
        return self._load()[0]

    def projection(self) -> Projection:
        return self._load()[1]

    def events(self) -> list[EventView]:
        """Base then meta event log, each in log order (what ``ik read`` prints)."""
        return self._load()[2]

    def write[T](self, author: Author, operation: Callable[[Archive], T]) -> T:
        """Run ``operation`` on a freshly verified archive signed by ``author``,
        one writer at a time."""
        with self._lock:
            archive = self._open(author)
            try:
                with library_errors():
                    return operation(archive)
            finally:
                self._cache = None

    def blob_path(self, blob_hash: str) -> Path:
        """Path of a stored blob; ``KeyError`` for malformed or absent hashes."""
        digest = _digest(blob_hash)
        if digest is None:
            raise KeyError(blob_hash)
        path = self.archive().index.store.blobs.path_for(digest)
        if not path.is_file():
            raise KeyError(blob_hash)
        return path
