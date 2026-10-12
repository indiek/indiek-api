"""FastAPI application: workspace-scoped read/write access to local IndieK archives.

Run with ``uv run python -m indiek_api`` (binds 127.0.0.1 only).
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from indiek import create_edge_tag, create_node_tag, set_tag, supersede_edge
from indiek.create_edge import create_base_edge
from indiek.create_node import (
    create_local_file_node,
    create_node_from_content,
    create_uri_node,
)
from indiek.models import MediaType
from indiek.supersede_node import create_supersede_event

from indiek_api import __version__
from indiek_api.archive_service import (
    ArchiveService,
    EdgeView,
    EventView,
    NodeView,
    Projection,
)
from indiek_api.errors import BlobNotFoundError, ItemNotFoundError, install_handlers
from indiek_api.schemas import (
    Created,
    EdgeCreate,
    EdgeOut,
    EdgeWrite,
    EventOut,
    GraphOut,
    LocalFileNodeCreate,
    ManagedNodeCreate,
    NodeCreate,
    NodeOut,
    NodeWrite,
    TagAssignment,
    TagCreate,
    TagOut,
    TargetTags,
    UriNodeCreate,
    WorkspaceCreate,
    WorkspaceOut,
)
from indiek_api.settings import Settings
from indiek_api.workspaces import WorkspaceRecord, WorkspaceRegistry


def _created(
    service: ArchiveService, new_ids: list[str], kind: Literal["node", "edge", "tag"]
) -> Created:
    projection = service.projection()
    pool = {"node": projection.nodes, "edge": projection.edges, "tag": projection.tags}[kind]
    item_id = next(item_id for item_id in new_ids if item_id in pool)
    return Created(id=item_id, alias=pool[item_id].alias)


def _find_item(projection: Projection, ref: str) -> NodeView | EdgeView:
    """Resolve a UUID or e{n} alias to a current base-network item."""
    for pool in (projection.nodes, projection.edges):
        if ref in pool:
            return pool[ref]
        for item in pool.values():
            if item.alias == ref:
                return item
    raise ItemNotFoundError(f"'{ref}' is not a node or edge of this archive")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.load_env_file()
    registry = WorkspaceRegistry(settings.registry_path)

    app = FastAPI(title="IndieK local API", version=__version__)
    install_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    api = APIRouter(prefix="/api/workspaces")

    # --- workspaces ----------------------------------------------------------

    @api.get("", response_model=list[WorkspaceOut])
    def list_workspaces() -> list[WorkspaceRecord]:
        return registry.list()

    @api.post("", response_model=WorkspaceOut, status_code=201)
    def register_workspace(payload: WorkspaceCreate, response: Response) -> WorkspaceRecord:
        record, created = registry.register(payload.path)
        if not created:
            response.status_code = 200
        return record

    @api.delete("/{workspace_id}", status_code=204)
    def remove_workspace(workspace_id: str) -> None:
        registry.remove(workspace_id)

    # --- reads ---------------------------------------------------------------

    @api.get("/{workspace_id}/graph", response_model=GraphOut)
    def graph(workspace_id: str) -> GraphOut:
        projection = registry.service(workspace_id).projection()
        return GraphOut(
            head=projection.head,
            meta_head=projection.meta_head,
            nodes=[NodeOut.model_validate(node) for node in projection.nodes.values()],
            edges=[EdgeOut.model_validate(edge) for edge in projection.edges.values()],
            tags=[TagOut.model_validate(tag) for tag in projection.tags.values()],
        )

    @api.get("/{workspace_id}/blobs/{blob_hash}")
    def blob(workspace_id: str, blob_hash: str) -> FileResponse:
        service = registry.service(workspace_id)
        try:
            path = service.blob_path(blob_hash)
        except KeyError:
            raise BlobNotFoundError(f"No blob '{blob_hash}' in this archive") from None
        media_type = service.projection().blob_media_types.get(path.name)
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    @api.get("/{workspace_id}/events", response_model=list[EventOut])
    def events(workspace_id: str) -> list[EventView]:
        return registry.service(workspace_id).events()

    # --- writes --------------------------------------------------------------

    @api.post("/{workspace_id}/nodes", response_model=Created, status_code=201)
    def create_node(workspace_id: str, payload: NodeCreate) -> Created:
        service = registry.service(workspace_id)
        author = settings.author_credentials()
        keys = (author.public_key, author.signing_key)
        match payload:
            case ManagedNodeCreate():
                new_ids = service.create(
                    lambda index: create_node_from_content(
                        index, payload.content, MediaType(payload.media_type), *keys, payload.title
                    )
                )
            case UriNodeCreate():
                new_ids = service.create(
                    lambda index: create_uri_node(
                        index, str(payload.uri), payload.title, payload.media_type, *keys
                    )
                )
            case LocalFileNodeCreate():
                new_ids = service.create(
                    lambda index: create_local_file_node(index, payload.path, payload.title, *keys)
                )
        return _created(service, new_ids, "node")

    @api.put("/{workspace_id}/nodes/{node_id}", response_model=Created)
    def supersede_node(workspace_id: str, node_id: str, payload: NodeWrite) -> Created:
        service = registry.service(workspace_id)
        author = settings.author_credentials()
        revised_id = service.write(
            lambda index: create_supersede_event(
                archive_idx=index,
                node_id=node_id,
                content=payload.content,
                title=payload.title,
                media_type=MediaType.TEXT_MARKDOWN,
                public_key=author.public_key,
                signing_key=author.signing_key,
            )
        )
        node = service.projection().nodes[str(revised_id)]
        return Created(id=node.id, alias=node.alias)

    @api.post("/{workspace_id}/edges", response_model=Created, status_code=201)
    def create_edge(workspace_id: str, payload: EdgeCreate) -> Created:
        service = registry.service(workspace_id)
        author = settings.author_credentials()
        new_ids = service.create(
            lambda index: create_base_edge(
                payload.content,
                payload.source,
                payload.target,
                author.public_key,
                author.signing_key,
                index,
            )
        )
        return _created(service, new_ids, "edge")

    @api.put("/{workspace_id}/edges/{edge_id}", response_model=Created)
    def supersede_edge_route(workspace_id: str, edge_id: str, payload: EdgeWrite) -> Created:
        service = registry.service(workspace_id)
        author = settings.author_credentials()
        revised_id = service.write(
            lambda index: supersede_edge.run(
                edge_id=edge_id,
                content=payload.content,
                curr_public_key=author.public_key,
                curr_signing_key=author.signing_key,
                archive_idx=index,
            )
        )
        edge = service.projection().edges[str(revised_id)]
        return Created(id=edge.id, alias=edge.alias)

    @api.post("/{workspace_id}/tags", response_model=Created, status_code=201)
    def create_tag(workspace_id: str, payload: TagCreate) -> Created:
        service = registry.service(workspace_id)
        author = settings.author_credentials()
        fields = {
            "tag_name": payload.name,
            "tag_description": payload.description,
            "curr_public_key": author.public_key,
            "curr_signing_key": author.signing_key,
        }
        if payload.kind == "node":
            new_ids = service.create(lambda index: create_node_tag.run(**fields, archive_idx=index))
        else:
            new_ids = service.create(
                lambda index: create_edge_tag.run(**fields, archive_index=index)
            )
        return _created(service, new_ids, "tag")

    @api.post(
        "/{workspace_id}/tags/{tag_id}/assignments", response_model=TargetTags, status_code=201
    )
    def assign_tag(workspace_id: str, tag_id: str, payload: TagAssignment) -> TargetTags:
        service = registry.service(workspace_id)
        author = settings.author_credentials()
        service.write(
            lambda index: set_tag.run(
                edge_source=tag_id,
                underlying_target=payload.target,
                curr_public_key=author.public_key,
                curr_signing_key=author.signing_key,
                archive_idx=index,
            )
        )
        target = _find_item(service.projection(), payload.target)
        return TargetTags(target=target.id, tags=list(target.tags))

    app.include_router(api)
    if settings.static_dir is not None and settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
