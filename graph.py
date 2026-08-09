"""Archive-to-graph projections used by the IndieK HTTP API."""

from __future__ import annotations

import logging
from pathlib import Path

import networkx as nx
from dotenv import load_dotenv

from ik.blob import Archive, ArchiveIndex, InfraBlobStore, InfraEventLog

from workspaces import resolve_archive_root

logger = logging.getLogger(__name__)
IK_CONFIG = Path(__file__).resolve().parent / ".env"
workspace_graphs: dict[str, nx.DiGraph] = {}
workspace_tags: dict[str, dict[str, dict[str, str]]] = {}


def initialize_archive(network_id: str | None = None) -> ArchiveIndex:
    """Open and verify the archive assigned to a workspace."""
    load_dotenv(IK_CONFIG, override=False)
    root = resolve_archive_root(network_id)
    return ArchiveIndex(
        Archive(InfraEventLog(root), InfraEventLog(root / "meta"), InfraBlobStore(root))
    )


def archive_last_event_hash(network_id: str | None = None) -> str | None:
    """Return the current event-chain head, or None for an empty archive."""
    try:
        return initialize_archive(network_id).last_event_hash_
    except (OSError, ValueError, KeyError) as exc:
        logger.warning("Could not read archive head for %s: %s", network_id, exc)
        return None


def invalidate_graph(network_id: str | None = None) -> None:
    """Discard cached graph and tag projections after an archive write."""
    workspace_id = network_id or "default"
    workspace_graphs.pop(workspace_id, None)
    workspace_tags.pop(workspace_id, None)


def load_graph_data(network_id: str | None = None) -> tuple[nx.DiGraph, dict[str, dict[str, str]]]:
    """Build or retrieve the current graph projection for one workspace."""
    workspace_id = network_id or "default"
    if workspace_id in workspace_graphs and workspace_id in workspace_tags:
        return workspace_graphs[workspace_id], workspace_tags[workspace_id]

    graph = nx.DiGraph()
    tags: dict[str, dict[str, str]] = {}
    try:
        archive_index = initialize_archive(workspace_id)
        for item_uuid, info in archive_index.id_info_.items():
            body = info["body"]
            if info["is_meta"]:
                if not info["is_edge"] and (info["is_edge_tag"] or info["is_node_tag"]):
                    tags[str(item_uuid)] = {"name": body.get("name", ""), "description": body.get("description", "")}
                continue
            attributes = {
                "blob_hash": body.get("blob_hash"),
                "tags": [str(tag) for tag in info["tags"]],
                "author": info.get("author"),
                "blob_mime": body.get("media_type"),
                "resource_type": body.get("resource_type", "managed"),
                "uri": body.get("uri"),
                "path": body.get("path"),
            }
            if info["is_edge"]:
                graph.add_edge(body["source_node"], body["target_node"], id=body["edge_id"], **attributes)
            else:
                graph.add_node(body["node_id"], title=body.get("title"), **attributes)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Could not build graph for workspace %s: %s", workspace_id, exc)

    workspace_graphs[workspace_id] = graph
    workspace_tags[workspace_id] = tags
    return graph, tags
