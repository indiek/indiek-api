"""FastAPI application: workspace-scoped read/write access to local IndieK archives.

Run with ``uv run python -m indiek_api`` (binds 127.0.0.1 only).
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from indiek import MediaType, ResourceType

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
    service: ArchiveService, item_id: UUID, kind: Literal["node", "edge", "tag"]
) -> Created:
    projection = service.projection()
    pool = {"node": projection.nodes, "edge": projection.edges, "tag": projection.tags}[kind]
    item = pool[str(item_id)]
    return Created(id=item.id, alias=item.alias)


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
        author = settings.author()
        match payload:
            case ManagedNodeCreate():
                node_id = service.write(
                    author,
                    lambda ik: ik.create_node(
                        payload.content,
                        media_type=MediaType(payload.media_type),
                        title=payload.title,
                    ),
                )
            case UriNodeCreate():
                node_id = service.write(
                    author,
                    lambda ik: ik.create_node(
                        resource_type=ResourceType.URI,
                        uri=str(payload.uri),
                        title=payload.title,
                        media_type=payload.media_type,
                    ),
                )
            case LocalFileNodeCreate():
                node_id = service.write(
                    author,
                    lambda ik: ik.create_node(
                        resource_type=ResourceType.LOCAL_FILE,
                        path=payload.path,
                        title=payload.title,
                    ),
                )
        return _created(service, node_id, "node")

    @api.put("/{workspace_id}/nodes/{node_id}", response_model=Created)
    def supersede_node(workspace_id: str, node_id: str, payload: NodeWrite) -> Created:
        service = registry.service(workspace_id)
        revised_id = service.write(
            settings.author(),
            lambda ik: ik.supersede_node(node_id, payload.content, title=payload.title),
        )
        return _created(service, revised_id, "node")

    @api.post("/{workspace_id}/edges", response_model=Created, status_code=201)
    def create_edge(workspace_id: str, payload: EdgeCreate) -> Created:
        service = registry.service(workspace_id)
        edge_id = service.write(
            settings.author(),
            lambda ik: ik.create_edge(payload.source, payload.target, payload.content),
        )
        return _created(service, edge_id, "edge")

    @api.put("/{workspace_id}/edges/{edge_id}", response_model=Created)
    def supersede_edge(workspace_id: str, edge_id: str, payload: EdgeWrite) -> Created:
        service = registry.service(workspace_id)
        revised_id = service.write(
            settings.author(), lambda ik: ik.supersede_edge(edge_id, payload.content)
        )
        return _created(service, revised_id, "edge")

    @api.post("/{workspace_id}/tags", response_model=Created, status_code=201)
    def create_tag(workspace_id: str, payload: TagCreate) -> Created:
        service = registry.service(workspace_id)
        tag_id = service.write(
            settings.author(),
            lambda ik: ik.create_tag(payload.name, payload.description, kind=payload.kind),
        )
        return _created(service, tag_id, "tag")

    @api.post(
        "/{workspace_id}/tags/{tag_id}/assignments", response_model=TargetTags, status_code=201
    )
    def assign_tag(workspace_id: str, tag_id: str, payload: TagAssignment) -> TargetTags:
        service = registry.service(workspace_id)
        service.write(settings.author(), lambda ik: ik.set_tag(tag_id, payload.target))
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
