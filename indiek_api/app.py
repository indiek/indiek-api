"""FastAPI application: workspace-scoped read/write access to local IndieK archives.

Run with ``uv run python -m indiek_api`` (binds 127.0.0.1 only).
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import webbrowser
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, BinaryIO, Literal
from uuid import UUID

from fastapi import APIRouter, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from indiek import Author, MediaType, ResourceType

from indiek_api import __version__
from indiek_api.archive_service import (
    ArchiveService,
    EdgeView,
    EventView,
    NodeView,
    Projection,
)
from indiek_api.errors import (
    BlobNotFoundError,
    InvalidKeyError,
    ItemNotFoundError,
    install_handlers,
)
from indiek_api.identity import KeyProvider
from indiek_api.providers import KeyManager, Signer, WorkspaceProvider
from indiek_api.schemas import (
    Created,
    EdgeCreate,
    EdgeOut,
    EdgeWrite,
    EventOut,
    GraphOut,
    IdentityBackup,
    IdentityGenerate,
    IdentityImport,
    IdentityOut,
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
from indiek_api.security import (
    SESSION_PATH,
    AccessControlMiddleware,
    AccessTokens,
    build_csp,
    inline_script_hashes,
)
from indiek_api.settings import Settings
from indiek_api.workspaces import WorkspaceRecord, WorkspaceRegistry

if TYPE_CHECKING:
    import uvicorn


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


def create_app(
    settings: Settings | None = None,
    *,
    workspaces: WorkspaceProvider | None = None,
    signer: Signer | None = None,
) -> FastAPI:
    """Build the API. ``workspaces`` and ``signer`` default to local folders
    (`WorkspaceRegistry`) and `KeyProvider`; see `indiek_api.providers`."""
    settings = settings or Settings.from_env()
    settings.load_env_file()
    registry = workspaces if workspaces is not None else WorkspaceRegistry(settings.registry_path)

    app = FastAPI(title="IndieK local API", version=__version__)
    install_handlers(app)
    access = AccessTokens(fixed=settings.token)
    app.state.access = access
    app.add_middleware(
        AccessControlMiddleware,
        tokens=access,
        port=settings.port,
        csp=build_csp(inline_script_hashes(settings.static_dir)),
        csp_report_only=settings.csp_report_only,
    )
    # Added last = outermost: answers CORS preflights (only for explicitly allowed origins).
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "api_version": __version__}

    # -- signing identity (DESKTOP:KEY-ONBOARDING) -------------------------------------

    identity: Signer = signer if signer is not None else KeyProvider()
    app.state.identity = identity
    if isinstance(identity, KeyManager):
        _add_identity_routes(app, identity)

    api = APIRouter(prefix="/api/workspaces")

    # --- workspaces ----------------------------------------------------------

    @api.get("", response_model=list[WorkspaceOut])
    def list_workspaces() -> list[WorkspaceRecord]:
        return registry.list()

    @api.post("", response_model=WorkspaceOut, status_code=201)
    def register_workspace(payload: WorkspaceCreate, response: Response) -> WorkspaceRecord:
        author = identity.author_or_none() if payload.create else None
        record, created = registry.register(payload.path, create=payload.create, author=author)
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
        author = identity.author()
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
            identity.author(),
            lambda ik: ik.supersede_node(node_id, payload.content, title=payload.title),
        )
        return _created(service, revised_id, "node")

    @api.post("/{workspace_id}/edges", response_model=Created, status_code=201)
    def create_edge(workspace_id: str, payload: EdgeCreate) -> Created:
        service = registry.service(workspace_id)
        edge_id = service.write(
            identity.author(),
            lambda ik: ik.create_edge(payload.source, payload.target, payload.content),
        )
        return _created(service, edge_id, "edge")

    @api.put("/{workspace_id}/edges/{edge_id}", response_model=Created)
    def supersede_edge(workspace_id: str, edge_id: str, payload: EdgeWrite) -> Created:
        service = registry.service(workspace_id)
        revised_id = service.write(
            identity.author(), lambda ik: ik.supersede_edge(edge_id, payload.content)
        )
        return _created(service, revised_id, "edge")

    @api.post("/{workspace_id}/tags", response_model=Created, status_code=201)
    def create_tag(workspace_id: str, payload: TagCreate) -> Created:
        service = registry.service(workspace_id)
        tag_id = service.write(
            identity.author(),
            lambda ik: ik.create_tag(payload.name, payload.description, kind=payload.kind),
        )
        return _created(service, tag_id, "tag")

    @api.post(
        "/{workspace_id}/tags/{tag_id}/assignments", response_model=TargetTags, status_code=201
    )
    def assign_tag(workspace_id: str, tag_id: str, payload: TagAssignment) -> TargetTags:
        service = registry.service(workspace_id)
        service.write(identity.author(), lambda ik: ik.set_tag(tag_id, payload.target))
        target = _find_item(service.projection(), payload.target)
        return TargetTags(target=target.id, tags=list(target.tags))

    app.include_router(api)
    if settings.static_dir is not None and settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="ui")
    return app


def _add_identity_routes(app: FastAPI, manager: KeyManager) -> None:
    """The signing-key management routes (DESKTOP:KEY-ONBOARDING)."""

    def identity_out() -> IdentityOut:
        author, source, available = manager.status()
        return IdentityOut(
            public_key=author.public_key_hex if author else None,
            source=source.value,
            keychain_available=available,
        )

    @app.get("/api/identity", response_model=IdentityOut)
    def get_identity() -> IdentityOut:
        return identity_out()

    @app.post("/api/identity/generate", response_model=IdentityOut, status_code=201)
    def generate_identity(payload: IdentityGenerate) -> IdentityOut:
        manager.store(Author.generate(), replace=payload.replace)
        return identity_out()

    @app.post("/api/identity/import", response_model=IdentityOut, status_code=201)
    def import_identity(payload: IdentityImport) -> IdentityOut:
        if (payload.seed_hex is None) == (payload.backup is None):
            raise InvalidKeyError("Send either seed_hex or a backup file with its password.")
        if payload.seed_hex is not None:
            try:
                author = Author.from_seed_hex(payload.seed_hex.strip())
            except ValueError:
                raise InvalidKeyError(
                    "The key must be a 32-byte Ed25519 seed written as 64 hex characters."
                ) from None
        else:
            if not payload.password:
                raise InvalidKeyError("A backup file needs its password.")
            author = manager.read_backup(payload.backup or "", payload.password)
        manager.store(author, replace=payload.replace)
        return identity_out()

    @app.post("/api/identity/backup")
    def backup_identity(payload: IdentityBackup) -> Response:
        author, data = manager.backup(payload.password)
        filename = f"indiek-key-{author.public_key_hex[:8]}.ikkey"
        return Response(
            content=data,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )


def bind_loopback(host: str, port: int | None) -> socket.socket:
    """A listening socket on ``host``; ``port=None`` lets the OS pick a free port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port or 0))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def launch_url(host: str, port: int, launch_token: str) -> str:
    return f"http://{host}:{port}{SESSION_PATH}?token={launch_token}"


def _stop_on_eof(stream: BinaryIO, server: uvicorn.Server) -> None:
    """Read (and discard) ``stream`` until EOF, then ask ``server`` to shut down."""
    while stream.read(4096):
        pass
    server.should_exit = True


def _serve(
    app: FastAPI,
    sockets: list[socket.socket],
    *,
    handshake: bool = False,
    exit_on_stdin_eof: bool = False,
) -> None:
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    # Handshake mode: stdout carries only the handshake line, so uvicorn's own logging
    # config (access log on stdout) is skipped and its loggers reach the root logger (stderr).
    log_config = None if handshake else LOGGING_CONFIG
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", log_config=log_config))
    if exit_on_stdin_eof:
        threading.Thread(target=_stop_on_eof, args=(sys.stdin.buffer, server), daemon=True).start()
    server.run(sockets=sockets)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m indiek_api", description="Serve the IndieK Desktop App on this machine."
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="print the launch URL without opening it"
    )
    parser.add_argument(
        "--handshake",
        action="store_true",
        help="for a parent process (desktop shell): print one JSON line "
        '{"indiek_api", "port", "launch_url"} on stdout, log to stderr, never open a browser',
    )
    parser.add_argument(
        "--exit-on-stdin-eof",
        action="store_true",
        help="shut down when stdin is closed (the parent process went away)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    sock = bind_loopback(settings.host, settings.port)
    try:
        settings = replace(settings, port=sock.getsockname()[1])
        app = create_app(settings)
        launch = app.state.access.launch
        assert settings.port is not None and launch is not None
        url = launch_url(settings.host, settings.port, launch)
        if args.handshake:
            line = {"indiek_api": __version__, "port": settings.port, "launch_url": url}
            print(json.dumps(line), flush=True)
        else:
            print(f"IndieK Desktop: open {url}", flush=True)
            if settings.open_browser and not args.no_browser:
                webbrowser.open(url)
        _serve(app, [sock], handshake=args.handshake, exit_on_stdin_eof=args.exit_on_stdin_eof)
    finally:
        sock.close()
