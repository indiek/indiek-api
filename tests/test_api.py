"""HTTP contract of the IndieK local API (workspace-scoped routes)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from indiek import Author

from indiek_api.settings import Settings

from conftest import SampleArchive, Workspace, open_archive, tamper_last_signature


def _graph(ws: Workspace) -> dict:
    response = ws.client.get(f"/api/workspaces/{ws.workspace_id}/graph")
    assert response.status_code == 200, response.text
    return response.json()


def _by_id(items: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in items}


def _assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert response.json()["detail"]


# --- health & workspaces -----------------------------------------------------


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_register_workspace_returns_its_identity(
    client: TestClient, sample_archive: SampleArchive
) -> None:
    response = client.post("/api/workspaces", json={"path": str(sample_archive.root)})
    assert response.status_code == 201
    body = response.json()
    assert body["path"] == str(sample_archive.root.resolve())
    assert body["name"] == sample_archive.root.name
    assert body["workspace_id"]


def test_registering_the_same_folder_twice_is_idempotent(workspace: Workspace) -> None:
    again = workspace.client.post("/api/workspaces", json={"path": str(workspace.sample.root)})
    assert again.status_code == 200
    assert again.json()["workspace_id"] == workspace.workspace_id
    assert len(workspace.client.get("/api/workspaces").json()) == 1


def test_register_missing_folder(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/workspaces", json={"path": str(tmp_path / "nope")})
    _assert_error(response, 404, "archive_not_found")


def test_register_tampered_archive_is_refused(
    client: TestClient, sample_archive: SampleArchive
) -> None:
    tamper_last_signature(sample_archive.root)
    response = client.post("/api/workspaces", json={"path": str(sample_archive.root)})
    _assert_error(response, 409, "archive_integrity")
    assert client.get("/api/workspaces").json() == []


def test_workspaces_survive_a_restart(workspace: Workspace, settings: Settings) -> None:
    from indiek_api.app import create_app

    with TestClient(create_app(settings)) as restarted:
        listed = restarted.get("/api/workspaces").json()
    assert [item["workspace_id"] for item in listed] == [workspace.workspace_id]


def test_remove_workspace_keeps_the_archive(workspace: Workspace) -> None:
    files_before = sorted(workspace.sample.root.rglob("*"))
    response = workspace.client.delete(f"/api/workspaces/{workspace.workspace_id}")
    assert response.status_code == 204
    assert workspace.client.get("/api/workspaces").json() == []
    assert sorted(workspace.sample.root.rglob("*")) == files_before
    _assert_error(
        workspace.client.get(f"/api/workspaces/{workspace.workspace_id}/graph"),
        404,
        "workspace_not_found",
    )


# --- reads -------------------------------------------------------------------


def test_graph_payload(workspace: Workspace, author: Author) -> None:
    graph = _graph(workspace)
    sample = workspace.sample
    assert graph["head"]

    nodes = _by_id(graph["nodes"])
    assert set(nodes) == {sample.node_a, sample.node_b}
    node_a = nodes[sample.node_a]
    assert node_a["alias"] == "e1"
    assert node_a["title"] == "A"
    assert node_a["author"] == author.public_key_hex
    assert node_a["resource_type"] == "managed"
    assert node_a["tags"] == [sample.node_tag]

    (edge,) = graph["edges"]
    assert (edge["id"], edge["alias"], edge["source"], edge["target"]) == (
        sample.edge,
        "e3",
        sample.node_a,
        sample.node_b,
    )

    (tag,) = graph["tags"]
    assert (tag["id"], tag["alias"], tag["name"], tag["kind"]) == (
        sample.node_tag,
        "m1",
        "theorem",
        "node",
    )


def test_blob_is_served_with_its_media_type(workspace: Workspace) -> None:
    node_b = _by_id(_graph(workspace)["nodes"])[workspace.sample.node_b]
    digest = node_b["blob_hash"].removeprefix("sha256:")
    response = workspace.client.get(f"/api/workspaces/{workspace.workspace_id}/blobs/{digest}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text == "# Lemma B"


@pytest.mark.parametrize("bad", ["0" * 64, "not-a-hash"])
def test_unknown_blob(workspace: Workspace, bad: str) -> None:
    response = workspace.client.get(f"/api/workspaces/{workspace.workspace_id}/blobs/{bad}")
    _assert_error(response, 404, "blob_not_found")


def test_unknown_workspace(client: TestClient) -> None:
    _assert_error(client.get("/api/workspaces/unknown/graph"), 404, "workspace_not_found")


# --- writes ------------------------------------------------------------------


def test_create_node(workspace: Workspace) -> None:
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/nodes",
        json={"title": "C", "content": "# Corollary C"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["alias"] == "e4"
    node = _by_id(_graph(workspace)["nodes"])[created["id"]]
    assert (node["title"], node["media_type"]) == ("C", "text/markdown; charset=utf-8")


def test_create_node_rejects_empty_content(workspace: Workspace) -> None:
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/nodes", json={"content": ""}
    )
    assert response.status_code == 422


def test_supersede_node(workspace: Workspace) -> None:
    node_id = workspace.sample.node_a
    response = workspace.client.put(
        f"/api/workspaces/{workspace.workspace_id}/nodes/{node_id}",
        json={"title": "A2", "content": "Theorem A, sharpened"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": node_id, "alias": "e1"}
    node = _by_id(_graph(workspace)["nodes"])[node_id]
    assert (node["title"], node["revised"]) == ("A2", True)


def test_supersede_unknown_node(workspace: Workspace) -> None:
    response = workspace.client.put(
        f"/api/workspaces/{workspace.workspace_id}/nodes/0190a000-0000-7000-8000-000000000000",
        json={"content": "x"},
    )
    _assert_error(response, 404, "item_not_found")


def test_create_edge_with_aliases(workspace: Workspace) -> None:
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/edges",
        json={"source": "e2", "target": "e1", "content": "supports"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["alias"] == "e4"
    edge = _by_id(_graph(workspace)["edges"])[created["id"]]
    assert (edge["source"], edge["target"]) == (workspace.sample.node_b, workspace.sample.node_a)


def test_create_edge_to_unknown_node(workspace: Workspace) -> None:
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/edges",
        json={"source": "e1", "target": "e99", "content": "?"},
    )
    _assert_error(response, 404, "item_not_found")


def test_create_tag(workspace: Workspace) -> None:
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/tags",
        json={"name": "uses", "description": "Depends on", "kind": "edge"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["alias"] == "m4"  # m2/m3 are the representation + set_node_tag
    tag = _by_id(_graph(workspace)["tags"])[created["id"]]
    assert (tag["name"], tag["description"], tag["kind"]) == ("uses", "Depends on", "edge")


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "x", "kind": "node"},
        {"name": "x", "description": "", "kind": "node"},
        {"name": "x", "description": "d", "kind": "other"},
    ],
)
def test_create_tag_validation(workspace: Workspace, payload: dict) -> None:
    response = workspace.client.post(f"/api/workspaces/{workspace.workspace_id}/tags", json=payload)
    assert response.status_code == 422


def test_assign_tag(workspace: Workspace) -> None:
    ws_url = f"/api/workspaces/{workspace.workspace_id}"
    response = workspace.client.post(
        f"{ws_url}/tags/{workspace.sample.node_tag}/assignments", json={"target": "e2"}
    )
    assert response.status_code == 201, response.text
    node_b = _by_id(_graph(workspace)["nodes"])[workspace.sample.node_b]
    assert node_b["tags"] == [workspace.sample.node_tag]


def test_assign_tag_kind_mismatch(workspace: Workspace) -> None:
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/tags/{workspace.sample.node_tag}/assignments",
        json={"target": workspace.sample.edge},
    )
    _assert_error(response, 422, "archive_rule")


def test_assign_another_authors_tag(workspace: Workspace, other_author: Author) -> None:
    open_archive(workspace.sample.root, other_author).create_tag("foreign", "Not ours", kind="node")
    foreign = next(t for t in _graph(workspace)["tags"] if t["name"] == "foreign")
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/tags/{foreign['id']}/assignments",
        json={"target": "e1"},
    )
    _assert_error(response, 403, "forbidden")


def test_writes_need_an_author_key(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IK_AUTHOR_PRIVATE_KEY")
    response = workspace.client.post(
        f"/api/workspaces/{workspace.workspace_id}/nodes", json={"content": "x"}
    )
    _assert_error(response, 503, "author_key_missing")
    assert len(_graph(workspace)["nodes"]) == 2
