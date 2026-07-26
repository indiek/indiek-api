import json
import sys
import logging
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import networkx as nx
from pydantic import BaseModel
from ik.blob import Archive, ArchiveIndex, InfraBlobStore, InfraEventLog

logger = logging.getLogger()
logger.addHandler(logging.StreamHandler(sys.stdout))

logger.setLevel(logging.DEBUG)  # set to logging.DEBUG if needed
logger.debug("DEBUGGING ACTIVATED")
IK_CONFIG = ".env"

try:
    import PIL  # Optional for future image conversion

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

app = FastAPI(title="Graph Data API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WORKSPACE_ROOT = Path(__file__).resolve().parent / ".workspaces"
WORKSPACE_ROOT.mkdir(exist_ok=True)

workspaces: Dict[str, Dict[str, Any]] = {}
workspace_graphs: Dict[str, nx.DiGraph] = {}
workspace_tags: Dict[str, Dict[str, Dict[str, str]]] = {}


class CreateWorkspaceURLRequest(BaseModel):
    sourceType: str
    url: Optional[str] = None


class CreateWorkspaceResponse(BaseModel):
    network_id: str


class GraphNode(BaseModel):
    id: str
    blob_hash: Optional[str] = None
    tags: Optional[list[str]] = None


class GraphLink(BaseModel):
    id: Optional[str] = None
    source: str
    target: str
    blob_hash: Optional[str] = None
    tags: Optional[list[str]] = None


class GraphResponse(BaseModel):
    nodes: list[GraphNode]
    links: list[GraphLink]


class TagInfo(BaseModel):
    name: str
    description: str


class TagDictionaryResponse(BaseModel):
    tags: Dict[str, TagInfo]


def _iso_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _workspace_dir(network_id: Optional[str] = None) -> Path:
    workspace_id = network_id or "default"
    return WORKSPACE_ROOT / workspace_id


def _resolve_archive_root(network_id: Optional[str] = None) -> Path:
    workspace_id = network_id or "default"
    workspace = workspaces.get(workspace_id)
    if workspace is None:
        workspace = _get_workspace_or_default(workspace_id)

    workspace_dir = _workspace_dir(workspace_id)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    archive_root = workspace.get("archive_root")
    if archive_root:
        return Path(archive_root).expanduser().resolve()

    archive_root_path = workspace_dir  # / "archive"
    archive_root_path.mkdir(parents=True, exist_ok=True)

    uploaded_file = workspace.get("uploaded_file")

    if workspace.get("source_type") == "upload" and uploaded_file:
        uploaded_path = Path(uploaded_file)
        if (
            uploaded_path.exists()
            and uploaded_path.is_file()
            and uploaded_path.suffix.lower() == ".zip"
        ):
            if not (archive_root_path / uploaded_path.stem).exists():
                with zipfile.ZipFile(uploaded_path, "r") as archive_zip:
                    archive_zip.extractall(archive_root_path)
    network_name = Path(uploaded_file).stem if uploaded_file else ""
    workspace["archive_root"] = str(archive_root_path / network_name)
    logger.debug(f"workspace['archive_root']={workspace['archive_root']}")
    return archive_root_path / network_name


def initialize_ik(network_id: Optional[str] = None) -> ArchiveIndex:
    """Initialize IndieK's archive for a given workspace."""
    load_dotenv(IK_CONFIG)

    rootp = _resolve_archive_root(network_id)
    logger.debug(f"{rootp=}")
    infra_blob = InfraBlobStore(archive_root=str(rootp))
    elog = InfraEventLog(str(rootp))
    meta_log = InfraEventLog(str(rootp / "meta"))
    archive = Archive(elog, meta_log, infra_blob)
    return ArchiveIndex(archive)


def _get_workspace_or_default(network_id: Optional[str] = None) -> Dict[str, Any]:
    workspace_id = network_id or "default"
    if workspace_id in workspaces:
        return workspaces[workspace_id]

    if workspace_id == "default":
        workspace = {
            "network_id": workspace_id,
            "source_type": "archive",
            "created_at": _iso_timestamp(),
            "source_url": None,
            "uploaded_file": None,
        }
        workspaces[workspace_id] = workspace
        return workspace

    raise HTTPException(status_code=404, detail="Workspace not found")


def load_graph_data(network_id: Optional[str] = None):
    """Populate graph data for a workspace.

    The backend currently uses the archive data as the canonical graph source and
    exposes it through workspace-scoped routes.
    """

    workspace_id = network_id or "default"
    if workspace_id in workspace_graphs and workspace_id in workspace_tags:
        return workspace_graphs[workspace_id], workspace_tags[workspace_id]

    graph = nx.DiGraph()
    tag_lookup: Dict[str, Dict[str, str]] = {}

    try:
        archive_idx = initialize_ik(workspace_id)

        def get_item_tags(item_info):
            body = item_info.get("body", {}) if isinstance(item_info, dict) else {}
            tags = body.get("tags", [])
            if not isinstance(tags, list):
                return []
            return [str(tag) for tag in tags]

        for item_uuid, item_info in archive_idx.id_info_.items():
            body = item_info.get("body", {})
            uuid_str = str(item_uuid)
            try:
                archive_idx.assert_node_is_tag(item_uuid)
                tag_lookup[uuid_str] = {
                    "name": body.get("name", ""),
                    "description": body.get("description", ""),
                }
            except AssertionError:
                if item_info.get("is_meta"):
                    continue
                if item_info.get("is_edge"):
                    graph.add_edge(
                        body.get("source_node"),
                        body.get("target_node"),
                        blob_hash=body.get("blob_hash"),
                        id=body.get("edge_id"),
                        tags=get_item_tags(item_info),
                    )
                else:
                    graph.add_node(
                        body.get("node_id"),
                        blob_hash=body.get("blob_hash"),
                        tags=get_item_tags(item_info),
                    )
    except KeyError, TypeError, ValueError, AssertionError:
        # Fall back to the empty graph if the archive contents are incomplete.
        pass

    workspace_graphs[workspace_id] = graph
    workspace_tags[workspace_id] = tag_lookup
    return graph, tag_lookup


async def _parse_workspace_payload(
    request: Request,
) -> tuple[str, Optional[str], Optional[UploadFile]]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form_data = await request.form()
        source_type = str(form_data.get("sourceType") or "upload")
        url = str(form_data.get("url") or "") or None
        uploaded_file = form_data.get("file")
        if uploaded_file is not None and hasattr(uploaded_file, "filename"):
            return source_type, url, uploaded_file
        return source_type, url, None

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}

    source_type = str(payload.get("sourceType") or "url")
    url = payload.get("url")
    return source_type, url if isinstance(url, str) else None, None


@app.on_event("startup")
def startup_event():
    load_graph_data("default")


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/api/workspaces", response_model=CreateWorkspaceResponse)
async def create_workspace(request: Request):
    source_type, url, uploaded_file = await _parse_workspace_payload(request)

    if source_type == "url":
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
    elif source_type == "upload":
        if uploaded_file is None:
            raise HTTPException(status_code=400, detail="file is required")
    else:
        raise HTTPException(
            status_code=400, detail="sourceType must be 'url' or 'upload'"
        )

    network_id = str(uuid.uuid4())
    workspace_dir = WORKSPACE_ROOT / network_id
    workspace_dir.mkdir(parents=True, exist_ok=True)

    workspace_record: Dict[str, Any] = {
        "network_id": network_id,
        "source_type": source_type,
        "created_at": _iso_timestamp(),
        "source_url": url,
        "uploaded_file": None,
    }

    if uploaded_file is not None:
        destination = workspace_dir / (uploaded_file.filename or "workspace-upload.zip")
        with destination.open("wb") as buffer:
            shutil.copyfileobj(uploaded_file.file, buffer)
        await uploaded_file.close()
        workspace_record["uploaded_file"] = str(destination)

    workspaces[network_id] = workspace_record
    _resolve_archive_root(network_id)
    load_graph_data(network_id)

    return CreateWorkspaceResponse(network_id=network_id)


@app.get("/api/workspaces")
def list_workspaces():
    return [
        {
            "network_id": workspace["network_id"],
            "source_type": workspace["source_type"],
            "created_at": workspace["created_at"],
        }
        for workspace in workspaces.values()
    ]


@app.get("/api/graph", response_model=GraphResponse)
def get_graph(network_id: Optional[str] = Query(default=None)):
    workspace = _get_workspace_or_default(network_id)
    graph, _ = load_graph_data(workspace["network_id"])

    nodes = []
    for node_id, data in graph.nodes(data=True):
        nodes.append(
            {
                "id": node_id,
                "blob_hash": data.get("blob_hash"),
                "tags": data.get("tags", []),
            }
        )

    links = []
    for source, target, data in graph.edges(data=True):
        links.append(
            {
                "id": data.get("id"),
                "source": source,
                "target": target,
                "blob_hash": data.get("blob_hash"),
                "tags": data.get("tags", []),
            }
        )

    return GraphResponse(nodes=nodes, links=links)


@app.get("/api/tags", response_model=TagDictionaryResponse)
def get_tags(network_id: Optional[str] = Query(default=None)):
    workspace = _get_workspace_or_default(network_id)
    _, tags = load_graph_data(workspace["network_id"])
    return TagDictionaryResponse(tags=tags)


@app.get("/api/blob/{hash_str}")
def get_blob(hash_str: str, network_id: Optional[str] = Query(default=None)):
    _get_workspace_or_default(network_id)

    load_dotenv(IK_CONFIG)
    rootp = _resolve_archive_root(network_id)
    infra_blob = InfraBlobStore(archive_root=str(rootp))

    dir1 = hash_str[0:2]
    dir2 = hash_str[2:4]
    base_dir = Path(infra_blob.blobs_path)
    file_path = base_dir / dir1 / dir2 / hash_str

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Blob not found")

    return FileResponse(file_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
