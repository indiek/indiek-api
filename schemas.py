"""Request and response schemas for the IndieK HTTP API."""

from typing import Optional

from pydantic import BaseModel, Field

from ik.models import MediaType


class CreateWorkspaceURLRequest(BaseModel):
    """JSON payload for a URL-backed workspace."""

    sourceType: str
    url: Optional[str] = None


class CreateWorkspaceResponse(BaseModel):
    """Identity returned after a workspace is created."""

    network_id: str


class GraphNode(BaseModel):
    """A current node projection for graph clients."""

    id: str
    blob_hash: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    title: Optional[str] = None
    author: Optional[str] = None
    blob_mime: Optional[str] = None
    resource_type: str = "managed"
    uri: Optional[str] = None
    path: Optional[str] = None


class GraphLink(BaseModel):
    """A current directed-edge projection for graph clients."""

    id: Optional[str] = None
    source: str
    target: str
    blob_hash: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    author: Optional[str] = None
    blob_mime: Optional[str] = None
    resource_type: str = "managed"


class GraphResponse(BaseModel):
    """Graph response payload."""

    nodes: list[GraphNode]
    links: list[GraphLink]


class TagInfo(BaseModel):
    """Human-readable tag information."""

    name: str
    description: str


class TagDictionaryResponse(BaseModel):
    """Mapping of tag UUIDs to their descriptions."""

    tags: dict[str, TagInfo]


class NodeWriteRequest(BaseModel):
    """Managed Markdown content sent by the node editor."""

    title: str | None = Field(default=None, max_length=500)
    content: str = Field(min_length=1)
    media_type: MediaType = MediaType.TEXT_MARKDOWN


class NodeMutationResponse(BaseModel):
    """Result of a node creation or supersede request."""

    node_id: str
    event_type: str
