"""Request and response models of the IndieK local API."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Discriminator, Field, Tag

from indiek import MediaType

# Values of indiek.MediaType that the node editor can produce.
EditorMediaType = Literal["text/markdown; charset=utf-8", "text/plain; charset=utf-8"]


class _FromView(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- requests ----------------------------------------------------------------


class WorkspaceCreate(BaseModel):
    path: str = Field(min_length=1, description="Absolute path of a local archive folder.")


class NodeWrite(BaseModel):
    """Managed text content; a supersede is always stored as Markdown by the library."""

    title: str | None = Field(default=None, max_length=500)
    content: str = Field(min_length=1)


class ManagedNodeCreate(NodeWrite):
    resource_type: Literal["managed"] = "managed"
    media_type: EditorMediaType = "text/markdown; charset=utf-8"


class UriNodeCreate(BaseModel):
    """A reference to an external URL; never fetched, no blob."""

    resource_type: Literal["uri"]
    uri: AnyUrl
    title: str | None = Field(default=None, max_length=500)
    media_type: MediaType | None = None


class LocalFileNodeCreate(BaseModel):
    """A pointer to a file on this machine; hashed once, never copied."""

    resource_type: Literal["local_file"]
    path: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=500)


def _resource_type(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("resource_type", "managed")
    return getattr(value, "resource_type", "managed")


NodeCreate = Annotated[
    Annotated[ManagedNodeCreate, Tag("managed")]
    | Annotated[UriNodeCreate, Tag("uri")]
    | Annotated[LocalFileNodeCreate, Tag("local_file")],
    Discriminator(_resource_type),
]


class EdgeWrite(BaseModel):
    content: str = Field(min_length=1)


class EdgeCreate(BaseModel):
    source: str = Field(min_length=1, description="Node UUID or e{n} alias.")
    target: str = Field(min_length=1, description="Node UUID or e{n} alias.")
    content: str = Field(min_length=1)


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    kind: Literal["node", "edge"]


class TagAssignment(BaseModel):
    target: str = Field(min_length=1, description="Node or edge UUID, or e{n} alias.")


# --- responses ---------------------------------------------------------------


class WorkspaceOut(_FromView):
    workspace_id: str
    name: str
    path: str
    created_at: str


class NodeOut(_FromView):
    id: str
    alias: str
    title: str | None
    author: str
    resource_type: str
    media_type: str | None
    blob_hash: str | None
    uri: str | None
    path: str | None
    file_size: int | None
    file_hash: str | None
    last_verified: str | None
    tags: list[str]
    revised: bool
    created_at: str
    updated_at: str


class EdgeOut(_FromView):
    id: str
    alias: str
    source: str
    target: str
    title: str | None
    author: str
    media_type: str | None
    blob_hash: str | None
    tags: list[str]
    revised: bool
    created_at: str
    updated_at: str
    content: str | None = Field(description="Edge text (plain-text blobs up to 64 KiB).")


class TagOut(_FromView):
    id: str
    alias: str
    name: str
    description: str
    kind: Literal["node", "edge"]
    author: str
    created_at: str
    updated_at: str


class EventOut(_FromView):
    network: Literal["base", "meta"]
    timestamp: str
    author: str
    event_type: str
    item_id: str | None
    alias: str | None


class GraphOut(BaseModel):
    head: str | None
    meta_head: str | None
    nodes: list[NodeOut]
    edges: list[EdgeOut]
    tags: list[TagOut]


class Created(BaseModel):
    id: str
    alias: str


class TargetTags(BaseModel):
    """Tags carried by a node or edge after an assignment."""

    target: str
    tags: list[str]
