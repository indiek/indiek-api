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

import pytest
from fastapi.testclient import TestClient

from indiek import Archive, Author, MediaType

from indiek_api.settings import Settings

# Fixed API token for tests (INDIEK_API_TOKEN equivalent); not a secret.
TEST_TOKEN = "test-token-0123456789abcdef"
# Headers the desktop UI sends: the token (Bearer, as the Tauri shell will) + the write header.
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}", "X-IndieK-Request": "1"}


def api_client(app: object) -> TestClient:
    """A TestClient that calls the API as the desktop UI does (loopback host, token, write header)."""
    return TestClient(app, base_url="http://127.0.0.1", headers=AUTH_HEADERS)  # type: ignore[arg-type]


# Built with the `ik` CLI by tests/fixtures/build_demo_archive.sh (see its header).
DEMO_ARCHIVE = Path(__file__).parent / "fixtures" / "demo_archive"


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


def open_archive(root: Path, author: Author | None = None) -> Archive:
    """Open and fully verify the archive at ``root``, as an outside writer (the CLI) would."""
    return Archive.open(root, author=author, verify="full")


def build_sample_archive(root: Path, author: Author) -> SampleArchive:
    """Write two nodes, one edge, one node tag and one tag assignment."""
    root.mkdir(parents=True, exist_ok=True)
    ik = open_archive(root, author)
    node_a = ik.create_node("Theorem A", media_type=MediaType.TEXT_PLAIN, title="A")
    node_b = ik.create_node("# Lemma B", media_type=MediaType.TEXT_MARKDOWN, title="B")
    edge = ik.create_edge(node_a, node_b, "uses")
    node_tag = ik.create_tag("theorem", "A proven statement", kind="node")
    ik.set_tag(node_tag, node_a)
    assert node_tag in open_archive(root).index.get(node_a).tags
    return SampleArchive(root, str(node_a), str(node_b), str(edge), str(node_tag))


def tamper_last_signature(root: Path) -> None:
    """Corrupt the signature of the newest base event in place."""
    log = sorted((root / "events").rglob("*ndjson"))[-1]
    lines = log.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    signature = event["signature"]  # "<sig_alg>:<hex>": flip a hex digit, keep the prefix
    event["signature"] = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    lines[-1] = json.dumps(event)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def verification_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the SDK's verification cache out of the developer's real cache dir."""
    cache = tmp_path / "ik-cache"
    monkeypatch.setenv("IK_CACHE_DIR", str(cache))
    return cache


@pytest.fixture
def author(monkeypatch: pytest.MonkeyPatch) -> Author:
    """The API's configured author: its seed is exported as IK_AUTHOR_PRIVATE_KEY."""
    current = Author.generate()
    monkeypatch.setenv("IK_AUTHOR_PRIVATE_KEY", current.seed_hex)
    monkeypatch.delenv("IK_ARCHIVE_PATH", raising=False)
    return current


@pytest.fixture
def other_author() -> Author:
    return Author.generate()


@pytest.fixture
def sample_archive(tmp_path: Path, author: Author) -> SampleArchive:
    return build_sample_archive(tmp_path / "archive", author)


@pytest.fixture
def demo_archive(tmp_path: Path) -> Path:
    """A private copy of the CLI-built demo archive, safe to mutate."""
    return Path(shutil.copytree(DEMO_ARCHIVE, tmp_path / "demo"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(env_file=None, registry_path=tmp_path / "workspaces.json", token=TEST_TOKEN)


@pytest.fixture
def client(settings: Settings, author: Author) -> Iterator[TestClient]:
    from indiek_api.app import create_app

    with api_client(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def workspace(client: TestClient, sample_archive: SampleArchive) -> Workspace:
    """The sample archive registered in place as a local-folder workspace."""
    response = client.post("/api/workspaces", json={"path": str(sample_archive.root)})
    assert response.status_code == 201, response.text
    return Workspace(client, response.json()["workspace_id"], sample_archive)


class MemoryKeyring:
    """In-memory stand-in for the OS keychain (keyring backend protocol)."""

    priority = 1

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        import keyring.errors

        if self.store.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError("not found")


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> MemoryKeyring:
    """No test ever touches the developer's real OS keychain."""
    fake = MemoryKeyring()
    monkeypatch.setattr("indiek.keys._default_backend", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def fast_key_backups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal Argon2id cost in tests (same code path; the real cost is ≈ 4 s)."""
    from nacl import pwhash

    monkeypatch.setattr("indiek.keys.BACKUP_OPSLIMIT", pwhash.argon2id.OPSLIMIT_MIN)
    monkeypatch.setattr("indiek.keys.BACKUP_MEMLIMIT", pwhash.argon2id.MEMLIMIT_MIN)
