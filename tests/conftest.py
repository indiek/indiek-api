"""Shared fixtures: hermetic archives, ephemeral author keys and an API client.

Nothing here reads a dotenv file or the developer's real archive: every test gets
a throwaway archive under ``tmp_path`` and an in-process Ed25519 seed.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey

from indiek import create_node_tag, set_tag
from indiek.blob import Archive, ArchiveIndex, InfraBlobStore, InfraEventLog
from indiek.create_edge import create_base_edge
from indiek.create_node import create_node_from_content
from indiek.models import MediaType
from indiek.signing import get_signing_key

from indiek_api.settings import Settings

# Built with the `ik` CLI by following the README walkthrough (every command once).
DEMO_ARCHIVE = Path(__file__).parent / "fixtures" / "demo_archive"


@dataclass(frozen=True)
class Author:
    seed_hex: str
    public_key: str
    signing_key: Any


@dataclass(frozen=True)
class SampleArchive:
    """IDs of the items written by :func:`build_sample_archive`."""

    root: Path
    node_a: str
    node_b: str
    edge: str
    node_tag: str


@dataclass(frozen=True)
class Workspace:
    client: TestClient
    workspace_id: str
    sample: SampleArchive


def make_author() -> Author:
    seed_hex = SigningKey.generate().encode().hex()
    signing_key = get_signing_key(seed_hex)
    return Author(seed_hex, signing_key.verify_key.encode().hex(), signing_key)


def open_index(root: Path) -> ArchiveIndex:
    """Open, verify and index the archive at ``root`` exactly as the CLI does."""
    return ArchiveIndex(
        Archive(InfraEventLog(root), InfraEventLog(root / "meta"), InfraBlobStore(root))
    )


def _only_id(index: ArchiveIndex, flag: str) -> str:
    matches = [str(uid) for uid, info in index.id_info_.items() if info.get(flag)]
    assert len(matches) == 1, f"expected exactly one item with {flag}, got {matches}"
    return matches[0]


def build_sample_archive(root: Path, author: Author) -> SampleArchive:
    """Write two nodes, one edge, one node tag and one tag assignment."""
    root.mkdir(parents=True, exist_ok=True)
    keys = (author.public_key, author.signing_key)
    node_a = create_node_from_content(
        open_index(root), "Theorem A", MediaType.TEXT_PLAIN, *keys, "A"
    )
    node_b = create_node_from_content(
        open_index(root), "# Lemma B", MediaType.TEXT_MARKDOWN, *keys, "B"
    )
    create_base_edge("uses", node_a, node_b, *keys, open_index(root))
    create_node_tag.run(
        tag_name="theorem",
        tag_description="A proven statement",
        curr_public_key=author.public_key,
        curr_signing_key=author.signing_key,
        archive_idx=open_index(root),
    )
    index = open_index(root)
    edge = next(
        str(uid) for uid, info in index.id_info_.items() if info["is_edge"] and not info["is_meta"]
    )
    node_tag = _only_id(index, "is_node_tag")
    set_tag.run(
        edge_source=node_tag,
        underlying_target=node_a,
        curr_public_key=author.public_key,
        curr_signing_key=author.signing_key,
        archive_idx=index,
    )
    assert UUID(node_tag) in open_index(root).id_info_[UUID(node_a)]["tags"]
    return SampleArchive(root, node_a, node_b, edge, node_tag)


def tamper_last_signature(root: Path) -> None:
    """Corrupt the signature of the newest base event in place."""
    log = sorted((root / "events").rglob("*ndjson"))[-1]
    lines = log.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    signature = event["signature"]
    event["signature"] = ("0" if signature[0] != "0" else "1") + signature[1:]
    lines[-1] = json.dumps(event)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def author(monkeypatch: pytest.MonkeyPatch) -> Author:
    """The API's configured author: its seed is exported as IK_AUTHOR_PRIVATE_KEY."""
    current = make_author()
    monkeypatch.setenv("IK_AUTHOR_PRIVATE_KEY", current.seed_hex)
    monkeypatch.delenv("IK_ARCHIVE_PATH", raising=False)
    return current


@pytest.fixture
def other_author() -> Author:
    return make_author()


@pytest.fixture
def sample_archive(tmp_path: Path, author: Author) -> SampleArchive:
    return build_sample_archive(tmp_path / "archive", author)


@pytest.fixture
def demo_archive(tmp_path: Path) -> Path:
    """A private copy of the CLI-built demo archive, safe to mutate."""
    return Path(shutil.copytree(DEMO_ARCHIVE, tmp_path / "demo"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(env_file=None, registry_path=tmp_path / "workspaces.json")


@pytest.fixture
def client(settings: Settings, author: Author) -> Iterator[TestClient]:
    from indiek_api.app import create_app

    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def workspace(client: TestClient, sample_archive: SampleArchive) -> Workspace:
    """The sample archive registered in place as a local-folder workspace."""
    response = client.post("/api/workspaces", json={"path": str(sample_archive.root)})
    assert response.status_code == 201, response.text
    return Workspace(client, response.json()["workspace_id"], sample_archive)
