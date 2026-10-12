"""Verified, cached and write-serialised access to one IndieK archive folder.

The archive is re-verified (chain, signatures, blobs) whenever its event logs
change on disk, whoever changed them: this API, the ``ik`` CLI or a ``git pull``.

Known limits:
- A blob altered on disk *without* any log change is only caught by the next full
  verification, i.e. after the next log change.
- The write lock serialises writers inside this process only; it does not guard
  against the ``ik`` CLI appending to the same archive at the same instant.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from indiek.blob import Archive, ArchiveIndex, InfraBlobStore, InfraEventLog

from indiek_api.errors import ArchiveNotFoundError, library_errors

_DIGEST = re.compile(r"[0-9a-f]{64}")
_LOGS: tuple[tuple[Literal["base", "meta"], str], ...] = (
    ("base", "events"),
    ("meta", "meta/events"),
)
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


def _read_events(root: Path, aliases: dict[str, str]) -> list[EventView]:
    """Read both logs: files in lexicographic order, lines in append order."""
    events: list[EventView] = []
    for network, log_dir in _LOGS:
        for path in sorted((root / log_dir).rglob("*ndjson"), key=str):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
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


def _edge_text(blobs_dir: Path, blob_hash: str | None, media_type: str | None) -> str | None:
    digest = _digest(blob_hash or "")
    if digest is None or not (media_type or "").startswith("text/plain"):
        return None
    path = blobs_dir / digest[:2] / digest[2:4] / digest
    if not path.is_file() or path.stat().st_size > _MAX_EDGE_TEXT:
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _project(index: ArchiveIndex, events: list[EventView], blobs_dir: Path) -> Projection:
    aliases: dict[str, str] = index.machine_to_human_id_
    times: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.item_id:
            created = times.get(event.item_id, (event.timestamp,))[0]
            times[event.item_id] = (created, event.timestamp)
    nodes: dict[str, NodeView] = {}
    edges: dict[str, EdgeView] = {}
    tags: dict[str, TagView] = {}
    media_types: dict[str, str] = {}

    for uid, info in index.id_info_.items():
        item_id, body = str(uid), info["body"]
        alias = aliases.get(item_id, "")
        created_at, updated_at = times.get(item_id, ("", ""))
        if info["is_meta"]:
            if info.get("is_node_tag") or info.get("is_edge_tag"):
                tags[item_id] = TagView(
                    id=item_id,
                    alias=alias,
                    name=body.get("name", ""),
                    description=body.get("description", ""),
                    kind="node" if info.get("is_node_tag") else "edge",
                    author=info["author"],
                    created_at=created_at,
                    updated_at=updated_at,
                )
            continue

        common: dict[str, Any] = {
            "id": item_id,
            "alias": alias,
            "title": body.get("title"),
            "author": info["author"],
            "media_type": body.get("media_type"),
            "blob_hash": body.get("blob_hash"),
            "tags": tuple(sorted(str(tag) for tag in info.get("tags") or ())),
            "revised": str(body.get("event_type", "")).startswith("supersede_"),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        if common["blob_hash"] and common["media_type"]:
            if digest := _digest(common["blob_hash"]):
                media_types[digest] = common["media_type"]

        if info["is_edge"]:
            edges[item_id] = EdgeView(
                source=str(body["source_node"]),
                target=str(body["target_node"]),
                content=_edge_text(blobs_dir, common["blob_hash"], common["media_type"]),
                **common,
            )
        else:
            nodes[item_id] = NodeView(
                resource_type=body.get("resource_type") or "managed",
                uri=body.get("uri"),
                path=body.get("path"),
                file_size=body.get("file_size"),
                file_hash=body.get("file_hash"),
                last_verified=body.get("last_verified"),
                **common,
            )

    return Projection(
        head=index.last_event_hash_,
        meta_head=index.last_meta_event_hash_,
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
        self._blobs_dir = Path(InfraBlobStore(root).blobs_path)
        self._lock = threading.RLock()
        self._cache: tuple[Fingerprint, ArchiveIndex, Projection, list[EventView]] | None = None

    def _fingerprint(self) -> Fingerprint:
        entries = []
        for _, log_dir in _LOGS:
            for path in (self.root / log_dir).rglob("*ndjson"):
                stat = path.stat()
                entries.append((str(path), stat.st_size, stat.st_mtime_ns))
        return tuple(sorted(entries))

    def _open(self) -> ArchiveIndex:
        """Open, verify and index the archive; raises ArchiveIntegrityError on tampering."""
        with library_errors():
            return ArchiveIndex(
                Archive(
                    InfraEventLog(self.root),
                    InfraEventLog(self.root / "meta"),
                    InfraBlobStore(self.root),
                )
            )

    def _load(self) -> tuple[ArchiveIndex, Projection, list[EventView]]:
        with self._lock:
            fingerprint = self._fingerprint()
            if self._cache is None or self._cache[0] != fingerprint:
                index = self._open()
                events = _read_events(self.root, index.machine_to_human_id_)
                self._cache = (fingerprint, index, _project(index, events, self._blobs_dir), events)
            return self._cache[1], self._cache[2], self._cache[3]

    def index(self) -> ArchiveIndex:
        """The cached index. Treat it as read-only; mutate through :meth:`write`."""
        return self._load()[0]

    def projection(self) -> Projection:
        return self._load()[1]

    def events(self) -> list[EventView]:
        """Base then meta event log, each in log order (what ``ik read`` prints)."""
        return self._load()[2]

    def write[T](self, operation: Callable[[ArchiveIndex], T]) -> T:
        """Run ``operation`` against a freshly verified index, one writer at a time."""
        with self._lock:
            index = self._open()
            try:
                with library_errors():
                    return operation(index)
            finally:
                self._cache = None

    def create(self, operation: Callable[[ArchiveIndex], object]) -> list[str]:
        """Run a creating write; return the IDs of the items it added, in creation order.

        The library does not return IDs for every creation (edges, tags), so they are
        found by diffing the index before and after, while still holding the lock.
        """
        with self._lock:
            before: set[object] = set()
            self.write(lambda index: (before.update(index.id_info_), operation(index)))
            after = self.index()
            return [str(uid) for uid in after.id_info_ if uid not in before]

    def blob_path(self, blob_hash: str) -> Path:
        """Path of a stored blob; ``KeyError`` for malformed or absent hashes."""
        digest = _digest(blob_hash)
        if digest is None:
            raise KeyError(blob_hash)
        path = self._blobs_dir / digest[:2] / digest[2:4] / digest
        if not path.is_file():
            raise KeyError(blob_hash)
        return path
