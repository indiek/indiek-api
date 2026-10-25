"""indiek 1.2.0 write lock (SDK2:WRITE-LOCK) seen through the API."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import indiek.sdk

from conftest import Workspace, open_archive


@contextmanager
def _lock_held_by_another_process(root: Path) -> Iterator[None]:
    """Hold ``<root>/.ik.lock`` from another process, as a running ``ik`` would."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from filelock import FileLock\n"
            "with FileLock(sys.argv[1]):\n"
            "    print('locked', flush=True); sys.stdin.read()",
            str(root / ".ik.lock"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        yield
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=30)


def test_a_write_while_another_process_holds_the_lock_is_409(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(indiek.sdk, "DEFAULT_LOCK_TIMEOUT", 0.2)
    url = f"/api/workspaces/{workspace.workspace_id}/nodes"
    before = open_archive(workspace.sample.root).fsck().base_count
    with _lock_held_by_another_process(workspace.sample.root):
        response = workspace.client.post(url, json={"content": "blocked"})
        assert (
            workspace.client.get(f"/api/workspaces/{workspace.workspace_id}/graph").status_code
            == 200
        )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "archive_locked"
    assert "being written by another process" in response.json()["detail"]
    assert open_archive(workspace.sample.root).fsck().base_count == before
    assert workspace.client.post(url, json={"content": "now free"}).status_code == 201
