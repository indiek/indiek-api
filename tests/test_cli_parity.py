"""API parity with the `ik` CLI: resource types, edge supersedes, timestamps, event log."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import Workspace


def _url(ws: Workspace, path: str) -> str:
    return f"/api/workspaces/{ws.workspace_id}{path}"


def _graph(ws: Workspace) -> dict:
    response = ws.client.get(_url(ws, "/graph"))
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(items: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in items}


def _assert_error(response, status: int, code: str | None = None) -> None:
    assert response.status_code == status, response.text
    if code is not None:
        assert response.json()["code"] == code


# --- timestamps --------------------------------------------------------------


def test_items_carry_creation_and_update_times(workspace: Workspace) -> None:
    graph = _graph(workspace)
    for item in [*graph["nodes"], *graph["edges"], *graph["tags"]]:
        assert item["created_at"].endswith("Z")
        assert item["updated_at"] >= item["created_at"]

    node_id = workspace.sample.node_a
    before = _by_id(graph["nodes"])[node_id]
    workspace.client.put(_url(workspace, f"/nodes/{node_id}"), json={"content": "revised"})
    after = _by_id(_graph(workspace)["nodes"])[node_id]
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] > before["updated_at"]


# --- edges -------------------------------------------------------------------


def test_edges_carry_their_text(workspace: Workspace) -> None:
    (edge,) = _graph(workspace)["edges"]
    assert edge["content"] == "uses"


def test_supersede_edge(workspace: Workspace) -> None:
    edge_id = workspace.sample.edge
    response = workspace.client.put(
        _url(workspace, f"/edges/{edge_id}"), json={"content": "relies on"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": edge_id, "alias": "e3"}

    edge = _by_id(_graph(workspace)["edges"])[edge_id]
    assert (edge["content"], edge["revised"]) == ("relies on", True)
    assert (edge["source"], edge["target"]) == (workspace.sample.node_a, workspace.sample.node_b)


def test_supersede_edge_by_alias(workspace: Workspace) -> None:
    response = workspace.client.put(_url(workspace, "/edges/e3"), json={"content": "x"})
    assert response.json()["id"] == workspace.sample.edge


def test_supersede_unknown_edge(workspace: Workspace) -> None:
    response = workspace.client.put(
        _url(workspace, "/edges/0190a000-0000-7000-8000-000000000000"), json={"content": "x"}
    )
    _assert_error(response, 404, "item_not_found")


def test_supersede_edge_with_a_node_id(workspace: Workspace) -> None:
    response = workspace.client.put(
        _url(workspace, f"/edges/{workspace.sample.node_a}"), json={"content": "x"}
    )
    _assert_error(response, 404, "item_not_found")


def test_supersede_edge_requires_content(workspace: Workspace) -> None:
    response = workspace.client.put(_url(workspace, "/edges/e3"), json={"content": ""})
    _assert_error(response, 422)


# --- uri nodes ---------------------------------------------------------------


def test_create_uri_node(workspace: Workspace) -> None:
    response = workspace.client.post(
        _url(workspace, "/nodes"),
        json={
            "resource_type": "uri",
            "uri": "https://example.org/paper.pdf",
            "title": "Paper",
            "media_type": "application/pdf; profile=pdfa",
        },
    )
    assert response.status_code == 201, response.text
    node = _by_id(_graph(workspace)["nodes"])[response.json()["id"]]
    assert node["resource_type"] == "uri"
    assert node["uri"] == "https://example.org/paper.pdf"
    assert node["title"] == "Paper"
    assert node["media_type"] == "application/pdf; profile=pdfa"
    assert node["blob_hash"] is None


def test_create_uri_node_without_media_type(workspace: Workspace) -> None:
    response = workspace.client.post(
        _url(workspace, "/nodes"), json={"resource_type": "uri", "uri": "https://example.org"}
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "payload",
    [
        {"resource_type": "uri"},
        {"resource_type": "uri", "uri": "not a url"},
        {"resource_type": "uri", "uri": "https://example.org", "media_type": "video/mp4"},
        {"resource_type": "other", "content": "x"},
    ],
)
def test_create_uri_node_validation(workspace: Workspace, payload: dict) -> None:
    _assert_error(workspace.client.post(_url(workspace, "/nodes"), json=payload), 422)


# --- local_file nodes --------------------------------------------------------


def test_create_local_file_node(workspace: Workspace, tmp_path: Path) -> None:
    note = tmp_path / "outside" / "note.md"
    note.parent.mkdir()
    note.write_text("# A local note\n", encoding="utf-8")

    response = workspace.client.post(
        _url(workspace, "/nodes"), json={"resource_type": "local_file", "path": str(note)}
    )
    assert response.status_code == 201, response.text
    node = _by_id(_graph(workspace)["nodes"])[response.json()["id"]]
    assert node["resource_type"] == "local_file"
    assert node["path"] == str(note.resolve())
    assert node["title"] == "note"
    assert node["media_type"] == "text/markdown; charset=utf-8"
    assert node["file_size"] == note.stat().st_size
    assert node["file_hash"].endswith(hashlib.sha256(note.read_bytes()).hexdigest())
    assert node["blob_hash"] is None
    assert not list(workspace.sample.root.rglob("note.md")), "local files are never copied"


def test_local_file_title_override(workspace: Workspace, tmp_path: Path) -> None:
    note = tmp_path / "n.txt"
    note.write_text("x", encoding="utf-8")
    response = workspace.client.post(
        _url(workspace, "/nodes"),
        json={"resource_type": "local_file", "path": str(note), "title": "Custom"},
    )
    assert _by_id(_graph(workspace)["nodes"])[response.json()["id"]]["title"] == "Custom"


def test_local_file_unsupported_type(workspace: Workspace, tmp_path: Path) -> None:
    odd = tmp_path / "data.xyz"
    odd.write_bytes(b"\x00\x01")
    response = workspace.client.post(
        _url(workspace, "/nodes"), json={"resource_type": "local_file", "path": str(odd)}
    )
    _assert_error(response, 422, "archive_rule")


def test_local_file_missing(workspace: Workspace, tmp_path: Path) -> None:
    response = workspace.client.post(
        _url(workspace, "/nodes"),
        json={"resource_type": "local_file", "path": str(tmp_path / "absent.md")},
    )
    _assert_error(response, 422, "archive_rule")


def test_managed_is_the_default_resource_type(workspace: Workspace) -> None:
    response = workspace.client.post(_url(workspace, "/nodes"), json={"content": "plain"})
    node = _by_id(_graph(workspace)["nodes"])[response.json()["id"]]
    assert node["resource_type"] == "managed"


# --- event log ---------------------------------------------------------------


def test_event_log_of_sample_archive(workspace: Workspace) -> None:
    response = workspace.client.get(_url(workspace, "/events"))
    assert response.status_code == 200, response.text
    events = response.json()

    base = [e for e in events if e["network"] == "base"]
    meta = [e for e in events if e["network"] == "meta"]
    assert [e["event_type"] for e in base] == ["create_node", "create_node", "create_edge"]
    assert [e["alias"] for e in base] == ["e1", "e2", "e3"]
    assert [e["event_type"] for e in meta] == [
        "create_node_tag",
        "create_node_representation",
        "set_node_tag",
    ]
    assert meta[0]["item_id"] == workspace.sample.node_tag
    for log in (base, meta):
        timestamps = [e["timestamp"] for e in log]
        assert timestamps == sorted(timestamps)
    assert {e["author"] for e in events} == {_graph(workspace)["nodes"][0]["author"]}


def test_event_log_grows_after_a_write(workspace: Workspace) -> None:
    before = len(workspace.client.get(_url(workspace, "/events")).json())
    workspace.client.put(_url(workspace, "/edges/e3"), json={"content": "x"})
    events = workspace.client.get(_url(workspace, "/events")).json()
    assert len(events) == before + 1
    last_base = [e for e in events if e["network"] == "base"][-1]
    assert (last_base["event_type"], last_base["alias"]) == ("supersede_edge", "e3")


def test_event_log_of_demo_archive(client: TestClient, demo_archive: Path) -> None:
    workspace_id = client.post("/api/workspaces", json={"path": str(demo_archive)}).json()[
        "workspace_id"
    ]
    events = client.get(f"/api/workspaces/{workspace_id}/events").json()
    kinds = Counter((e["network"], e["event_type"].split("_")[0]) for e in events)
    assert kinds[("base", "create")] == 10
    assert kinds[("meta", "create")] + kinds[("meta", "set")] == 6
    superseded = {e["alias"] for e in events if e["event_type"].startswith("supersede_")}
    assert {"e1", "e8"} <= superseded
