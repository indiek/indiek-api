"""DESKTOP:REPO-SPLIT-3 + DESKTOP:REUSE-SEAMS (owner decisions 2026-10-30).

- Reuse seams: ``create_app`` accepts any ``WorkspaceProvider`` and ``Signer``; the
  ``/api/identity/*`` routes exist only when the signer can manage keys (``KeyManager``).
- Shell seams: ``/health`` reports ``api_version``; ``--handshake`` prints one JSON line on
  stdout and never opens a browser; ``--exit-on-stdin-eof`` stops the server when the parent
  closes stdin.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from indiek import Author

from indiek_api import __version__
from indiek_api.app import create_app, main
from indiek_api.archive_service import ArchiveService
from indiek_api.identity import KeyProvider
from indiek_api.providers import KeyManager, Signer, WorkspaceProvider
from indiek_api.settings import Settings
from indiek_api.workspaces import WorkspaceRecord, WorkspaceRegistry

from conftest import SampleArchive, api_client

PROJECT_DIR = Path(__file__).resolve().parent.parent


class PlainSigner:
    """A signer with a fixed author and no key management (an embedder's own signer)."""

    def __init__(self, author: Author) -> None:
        self._author = author

    def author(self) -> Author:
        return self._author

    def author_or_none(self) -> Author | None:
        return self._author


class RecordingWorkspaces:
    """Delegates to a real registry and records which methods the routes call."""

    def __init__(self, registry: WorkspaceRegistry) -> None:
        self._registry = registry
        self.calls: list[str] = []

    def list(self) -> list[WorkspaceRecord]:
        self.calls.append("list")
        return self._registry.list()

    def register(
        self, path: str | Path, *, create: bool = False, author: Author | None = None
    ) -> tuple[WorkspaceRecord, bool]:
        self.calls.append("register")
        return self._registry.register(path, create=create, author=author)

    def remove(self, workspace_id: str) -> None:
        self.calls.append("remove")
        self._registry.remove(workspace_id)

    def service(self, workspace_id: str) -> ArchiveService:
        self.calls.append("service")
        return self._registry.service(workspace_id)


# --- reuse seams -----------------------------------------------------------------------


def test_default_implementations_satisfy_the_protocols(tmp_path: Path) -> None:
    assert isinstance(WorkspaceRegistry(tmp_path / "ws.json"), WorkspaceProvider)
    provider = KeyProvider()
    assert isinstance(provider, Signer)
    assert isinstance(provider, KeyManager)


def test_plain_signer_is_not_a_key_manager(author: Author) -> None:
    signer = PlainSigner(author)
    assert isinstance(signer, Signer)
    assert not isinstance(signer, KeyManager)


def test_default_app_keeps_identity_routes(client: TestClient) -> None:
    assert client.get("/api/identity").status_code == 200


def test_injected_signer_and_workspaces_are_used(
    settings: Settings, sample_archive: SampleArchive, other_author: Author
) -> None:
    workspaces = RecordingWorkspaces(WorkspaceRegistry(settings.registry_path))
    app = create_app(settings, workspaces=workspaces, signer=PlainSigner(other_author))
    with api_client(app) as client:
        created = client.post("/api/workspaces", json={"path": str(sample_archive.root)})
        assert created.status_code == 201, created.text
        workspace_id = created.json()["workspace_id"]
        node = client.post(f"/api/workspaces/{workspace_id}/nodes", json={"content": "x"})
        assert node.status_code == 201, node.text
        graph = client.get(f"/api/workspaces/{workspace_id}/graph").json()
        assert client.get("/api/workspaces").status_code == 200
        assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 204

    # The write was signed by the injected signer, not by IK_AUTHOR_PRIVATE_KEY.
    new = next(n for n in graph["nodes"] if n["id"] == node.json()["id"])
    assert new["author"] == other_author.public_key_hex
    assert set(workspaces.calls) == {"register", "service", "list", "remove"}


def test_identity_routes_absent_without_key_management(settings: Settings, author: Author) -> None:
    with api_client(create_app(settings, signer=PlainSigner(author))) as client:
        assert client.get("/api/identity").status_code == 404
        assert client.post("/api/identity/generate", json={}).status_code == 404


# --- shell seams -----------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Environment for running ``main``: no dotenv file, throwaway registry, random port."""
    values = {
        "INDIEK_API_ENV_FILE": str(tmp_path / "absent.env"),
        "INDIEK_API_REGISTRY": str(tmp_path / "workspaces.json"),
        "INDIEK_API_OPEN_BROWSER": "1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    for name in ("INDIEK_API_PORT", "INDIEK_API_TOKEN", "INDIEK_API_STATIC_DIR"):
        monkeypatch.delenv(name, raising=False)
    return {**os.environ, **values}


def test_handshake_prints_one_json_line_and_no_browser(
    isolated_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served: list[dict[str, bool]] = []
    monkeypatch.setattr("indiek_api.app._serve", lambda app, sockets, **kw: served.append(kw))
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("browser opened"))

    main(["--handshake"])

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    handshake = json.loads(lines[0])
    assert set(handshake) == {"indiek_api", "port", "launch_url"}
    assert handshake["indiek_api"] == __version__
    assert isinstance(handshake["port"], int) and handshake["port"] > 0
    assert handshake["launch_url"].startswith(f"http://127.0.0.1:{handshake['port']}/api/session?")
    assert served == [{"handshake": True, "exit_on_stdin_eof": False}]


def test_default_launch_still_prints_and_opens_browser(
    isolated_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("indiek_api.app._serve", lambda app, sockets, **kw: None)
    monkeypatch.setattr("webbrowser.open", opened.append)

    main([])

    out = capsys.readouterr().out
    assert out.startswith("IndieK Desktop: open http://127.0.0.1:")
    assert len(opened) == 1 and opened[0] in out


def _read_line(stream: object, timeout: float) -> bytes:
    ready, _, _ = select.select([stream], [], [], timeout)
    assert ready, "no handshake line before timeout"
    return stream.readline()  # type: ignore[attr-defined, no-any-return]


@pytest.mark.skipif(sys.platform == "win32", reason="select() on pipes is POSIX-only")
def test_sidecar_handshake_then_exits_on_stdin_eof(isolated_env: dict[str, str]) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "indiek_api", "--handshake", "--exit-on-stdin-eof"],
        cwd=PROJECT_DIR,
        env=isolated_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        handshake = json.loads(_read_line(proc.stdout, timeout=20))
        health_url = f"http://127.0.0.1:{handshake['port']}/health"
        with urllib.request.urlopen(health_url, timeout=10) as response:
            assert json.load(response)["api_version"] == __version__

        proc.stdin.close()  # the shell went away
        assert proc.wait(timeout=20) == 0
        # Logs (including the access log of /health) went to stderr, never after the handshake.
        assert proc.stdout.read() == b""
        assert b"/health" in (proc.stderr.read() if proc.stderr else b"")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
