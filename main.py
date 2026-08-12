"""FastAPI application exposing writable IndieK archive workspaces."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import re
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from ik.blob import InfraBlobStore
from ik.create_node import create_node_from_content
from ik.create_edge import create_base_edge
import ik.create_node_tag as create_node_tag
import ik.create_edge_tag as create_edge_tag
import ik.set_tag as set_tag
from ik.signing import get_signing_key
from ik.supersede_node import create_supersede_event

import graph
import workspaces
from schemas import (
    CreateWorkspaceResponse,
    EdgeMutationResponse,
    EdgeWriteRequest,
    GraphResponse,
    NodeMutationResponse,
    NodeWriteRequest,
    SetTagRequest,
    SetTagResponse,
    TagCreateRequest,
    TagCreateResponse,
    TagDictionaryResponse,
)

logger = logging.getLogger("indiek.api")
if not logger.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

ENV_FILE = Path(__file__).resolve().parent / ".env"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Restore persistent workspaces on startup."""
    workspaces.load_all_workspaces()
    yield


app = FastAPI(title="IndieK Graph Data API", version="0.9.0", lifespan=lifespan)

cors_origins = [origin.strip() for origin in os.getenv("IK_CORS_ORIGINS", "http://localhost:3000,http://localhost:3001").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _writer_credentials():
    """Load the configured Ed25519 author key for archive write operations."""
    load_dotenv(ENV_FILE, override=False)
    private_key = os.getenv("IK_AUTHOR_PRIVATE_KEY")
    if not private_key:
        raise HTTPException(
            status_code=503,
            detail="Archive writing requires IK_AUTHOR_PRIVATE_KEY in the API environment.",
        )
    try:
        signing_key = get_signing_key(private_key)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="IK_AUTHOR_PRIVATE_KEY is invalid.") from exc
    public_key = signing_key.verify_key.encode().hex()
    return public_key, signing_key


def _refresh_workspace_after_write(network_id: str) -> None:
    """Invalidate projections and persist the latest archive chain head."""
    graph.invalidate_graph(network_id)
    workspaces.record_archive_head(network_id, graph.archive_last_event_hash(network_id))


@app.get("/health")
def health_check() -> dict[str, str]:
    """Return a lightweight readiness response."""
    return {"status": "ok"}


@app.post("/api/workspaces", response_model=CreateWorkspaceResponse)
async def create_workspace(request: Request) -> CreateWorkspaceResponse:
    """Create a URL or uploaded-archive workspace."""
    record = await workspaces.create_workspace(request)
    network_id = record["network_id"]
    try:
        workspaces.record_archive_head(network_id, graph.archive_last_event_hash(network_id))
        graph.load_graph_data(network_id)
    except HTTPException:
        raise
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("Workspace %s was created but could not be opened as an archive: %s", network_id, exc)
    return CreateWorkspaceResponse(network_id=network_id)


@app.get("/api/workspaces")
def list_workspaces() -> list[dict[str, object]]:
    """List persistent workspaces available to the graph explorer."""
    return workspaces.list_workspace_summaries()


@app.get("/api/graph", response_model=GraphResponse)
def get_graph(network_id: str | None = Query(default=None)) -> GraphResponse:
    """Return the current graph projection for one workspace."""
    workspace = workspaces.get_workspace(network_id)
    network_id_clean = workspace["network_id"]
    graph_data, _ = graph.load_graph_data(network_id_clean)
    return GraphResponse(
        nodes=[
            {
                "id": node_id,
                "blob_hash": data.get("blob_hash"),
                "tags": data.get("tags", []),
                "title": data.get("title"),
                "author": data.get("author"),
                "blob_mime": data.get("blob_mime"),
                "resource_type": data.get("resource_type", "managed"),
                "uri": data.get("uri"),
                "path": data.get("path"),
            }
            for node_id, data in graph_data.nodes(data=True)
        ],
        links=[
            {
                "id": data.get("id"),
                "source": source,
                "target": target,
                "blob_hash": data.get("blob_hash"),
                "tags": data.get("tags", []),
                "author": data.get("author"),
                "blob_mime": data.get("blob_mime"),
                "resource_type": data.get("resource_type", "managed"),
            }
            for source, target, data in graph_data.edges(data=True)
        ],
    )


@app.get("/api/tags", response_model=TagDictionaryResponse)
def get_tags(network_id: str | None = Query(default=None)) -> TagDictionaryResponse:
    """Return tag definitions for one workspace."""
    workspace = workspaces.get_workspace(network_id)
    _, tags = graph.load_graph_data(workspace["network_id"])
    return TagDictionaryResponse(tags=tags)


@app.post("/api/nodes", response_model=NodeMutationResponse, status_code=201)
def create_node(payload: NodeWriteRequest, network_id: str | None = Query(default=None)) -> NodeMutationResponse:
    """Create a managed node from archival-safe editor content."""
    workspace = workspaces.get_workspace(network_id)
    public_key, signing_key = _writer_credentials()
    try:
        node_id = create_node_from_content(
            graph.initialize_archive(workspace["network_id"]),
            payload.content,
            payload.media_type,
            public_key,
            signing_key,
            payload.title,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _refresh_workspace_after_write(workspace["network_id"])
    return NodeMutationResponse(node_id=node_id, event_type="create_node")


@app.put("/api/nodes/{node_id}", response_model=NodeMutationResponse)
def supersede_node(node_id: str, payload: NodeWriteRequest, network_id: str | None = Query(default=None)) -> NodeMutationResponse:
    """Append managed replacement content for an existing node."""
    workspace = workspaces.get_workspace(network_id)
    public_key, signing_key = _writer_credentials()
    try:
        updated_node_id = create_supersede_event(
            archive_idx=graph.initialize_archive(workspace["network_id"]),
            node_id=node_id,
            content=payload.content,
            title=payload.title,
            media_type=payload.media_type,
            public_key=public_key,
            signing_key=signing_key,
        )
    except ValueError as exc:
        status = 404 if "does not exist" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    _refresh_workspace_after_write(workspace["network_id"])
    return NodeMutationResponse(node_id=updated_node_id, event_type="supersede_node")


@app.post("/api/edges", response_model=EdgeMutationResponse, status_code=201)
def create_edge_endpoint(payload: EdgeWriteRequest, network_id: str | None = Query(default=None)) -> EdgeMutationResponse:
    """Create a directed edge in the base network of the workspace archive."""
    workspace = workspaces.get_workspace(network_id)
    public_key, signing_key = _writer_credentials()
    archive_idx = graph.initialize_archive(workspace["network_id"])
    try:
        create_base_edge(
            edge_content=payload.content,
            edge_source=payload.source_node,
            edge_target=payload.target_node,
            curr_public_key=public_key,
            curr_signing_key=signing_key,
            archive_idx=archive_idx,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _refresh_workspace_after_write(workspace["network_id"])
    return EdgeMutationResponse(status="ok")


@app.post("/api/tags", response_model=TagCreateResponse, status_code=201)
def create_tag_endpoint(payload: TagCreateRequest, network_id: str | None = Query(default=None)) -> TagCreateResponse:
    """Create a new tag definition (node or edge tag)."""
    workspace = workspaces.get_workspace(network_id)
    public_key, signing_key = _writer_credentials()
    archive_idx = graph.initialize_archive(workspace["network_id"])
    try:
        if payload.tag_type == "edge":
            create_edge_tag.run(
                tag_name=payload.name,
                tag_description=payload.description,
                curr_public_key=public_key,
                curr_signing_key=signing_key,
                archive_index=archive_idx,
            )
        else:
            create_node_tag.run(
                tag_name=payload.name,
                tag_description=payload.description,
                curr_public_key=public_key,
                curr_signing_key=signing_key,
                archive_idx=archive_idx,
            )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _refresh_workspace_after_write(workspace["network_id"])
    return TagCreateResponse(status="ok")


@app.post("/api/tags/set", response_model=SetTagResponse, status_code=200)
def set_tag_endpoint(payload: SetTagRequest, network_id: str | None = Query(default=None)) -> SetTagResponse:
    """Assign a tag to a target node or edge in the workspace."""
    workspace = workspaces.get_workspace(network_id)
    public_key, signing_key = _writer_credentials()
    archive_idx = graph.initialize_archive(workspace["network_id"])
    try:
        set_tag.run(
            edge_source=payload.tag_id,
            underlying_target=payload.target_id,
            curr_public_key=public_key,
            curr_signing_key=signing_key,
            archive_idx=archive_idx,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _refresh_workspace_after_write(workspace["network_id"])
    return SetTagResponse(status="ok")


@app.get("/api/blob/{hash_str}")
def get_blob(hash_str: str, network_id: str | None = Query(default=None)) -> FileResponse:
    """Stream a managed blob using its MIME type from the current projection."""
    if not re.fullmatch(r"[0-9a-f]{64}", hash_str):
        raise HTTPException(status_code=400, detail="Blob hash must be 64 lower-case hexadecimal characters")
    workspace = workspaces.get_workspace(network_id)
    root = workspaces.resolve_archive_root(workspace["network_id"])
    blob_store = InfraBlobStore(root)
    file_path = Path(blob_store.blobs_path) / hash_str[:2] / hash_str[2:4] / hash_str
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Blob not found")

    media_type: str | None = None
    graph_data, _ = graph.load_graph_data(workspace["network_id"])
    for _, data in list(graph_data.nodes(data=True)) + [(_, data) for _, _, data in graph_data.edges(data=True)]:
        if (data.get("blob_hash") or "").endswith(hash_str):
            media_type = data.get("blob_mime")
            break
    return FileResponse(file_path, media_type=media_type)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

