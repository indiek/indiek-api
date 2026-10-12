"""Unit tests for ArchiveService: projection, caching, integrity and write serialisation."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from indiek.create_node import create_node_from_content
from indiek.models import MediaType
from indiek.supersede_node import create_supersede_event

from indiek_api.archive_service import ArchiveService
from indiek_api.errors import ArchiveIntegrityError, ArchiveNotFoundError

from conftest import Author, SampleArchive, open_index, tamper_last_signature


# --- projection --------------------------------------------------------------


def test_projection_of_sample_archive(sample_archive: SampleArchive, author: Author) -> None:
    projection = ArchiveService(sample_archive.root).projection()

    assert set(projection.nodes) == {sample_archive.node_a, sample_archive.node_b}
    assert set(projection.edges) == {sample_archive.edge}
    assert set(projection.tags) == {sample_archive.node_tag}

    node_a = projection.nodes[sample_archive.node_a]
    assert (node_a.alias, node_a.title, node_a.author) == ("e1", "A", author.public_key)
    assert node_a.resource_type == "managed"
    assert node_a.media_type == "text/plain; charset=utf-8"
    assert node_a.tags == (sample_archive.node_tag,)
    assert node_a.revised is False
    assert projection.nodes[sample_archive.node_b].alias == "e2"

    edge = projection.edges[sample_archive.edge]
    assert (edge.alias, edge.source, edge.target) == (
        "e3",
        sample_archive.node_a,
        sample_archive.node_b,
    )

    tag = projection.tags[sample_archive.node_tag]
    assert (tag.alias, tag.name, tag.description, tag.kind) == (
        "m1",
        "theorem",
        "A proven statement",
        "node",
    )
    assert projection.head is not None


def test_representations_and_tag_edges_are_not_exposed(sample_archive: SampleArchive) -> None:
    projection = ArchiveService(sample_archive.root).projection()
    # m2 (representation) and m3 (set_node_tag) are bookkeeping, not tags.
    assert [tag.alias for tag in projection.tags.values()] == ["m1"]


def test_empty_folder_is_an_empty_archive(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    projection = ArchiveService(root).projection()
    assert (projection.nodes, projection.edges, projection.tags) == ({}, {}, {})
    assert projection.head is None


def test_missing_folder_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArchiveNotFoundError):
        ArchiveService(tmp_path / "does-not-exist")


# --- demo archive built by the real CLI -------------------------------------


def _by_alias(items: dict) -> dict:
    return {item.alias: item for item in items.values()}


def test_demo_archive_resource_types(demo_archive: Path) -> None:
    nodes = _by_alias(ArchiveService(demo_archive).projection().nodes)

    survey = nodes["e5"]
    assert (survey.resource_type, survey.uri, survey.title) == (
        "uri",
        "https://adrianradillo.org/media/pdfs/phd_dissertation_radillo_2018.pdf",
        "PhD thesis",
    )
    assert survey.blob_hash is None

    scan = nodes["e6"]
    assert scan.resource_type == "local_file"
    assert scan.path is not None and Path(scan.path).is_absolute()
    assert scan.file_hash and scan.file_size and scan.blob_hash is None

    assert {node.resource_type for node in nodes.values()} == {"managed", "uri", "local_file"}


def test_demo_archive_supersedes_keep_identity(demo_archive: Path) -> None:
    service = ArchiveService(demo_archive)
    projection = service.projection()
    nodes, edges = _by_alias(projection.nodes), _by_alias(projection.edges)

    theorem = nodes["e1"]
    assert theorem.revised is True
    assert theorem.media_type == "text/markdown; charset=utf-8"
    assert service.blob_path(theorem.blob_hash).read_text(encoding="utf-8") == (
        "Gödel's *first* incompleteness theorem"
    )

    proof = edges["e8"]
    assert proof.revised is True
    assert service.blob_path(proof.blob_hash).read_text(encoding="utf-8") == ("sketches a proof of")
    assert (proof.source, proof.target) == (nodes["e3"].id, nodes["e2"].id)


def test_demo_archive_tags(demo_archive: Path) -> None:
    projection = ArchiveService(demo_archive).projection()
    tags = _by_alias(projection.tags)
    assert (tags["m1"].name, tags["m1"].kind) == ("theorem", "node")
    assert (tags["m2"].name, tags["m2"].kind) == ("proof", "edge")
    assert _by_alias(projection.nodes)["e1"].tags == (tags["m1"].id,)
    assert _by_alias(projection.edges)["e8"].tags == (tags["m2"].id,)


# --- blobs -------------------------------------------------------------------


def test_blob_path_accepts_prefixed_and_bare_hashes(sample_archive: SampleArchive) -> None:
    service = ArchiveService(sample_archive.root)
    blob_hash = service.projection().nodes[sample_archive.node_a].blob_hash
    assert blob_hash.startswith("sha256:")
    assert service.blob_path(blob_hash) == service.blob_path(blob_hash.removeprefix("sha256:"))
    assert service.blob_path(blob_hash).read_text(encoding="utf-8") == "Theorem A"


@pytest.mark.parametrize("bad", ["", "xyz", "../../etc/passwd", "A" * 64, "0" * 64])
def test_blob_path_rejects_malformed_or_unknown_hashes(
    sample_archive: SampleArchive, bad: str
) -> None:
    with pytest.raises(KeyError):
        ArchiveService(sample_archive.root).blob_path(bad)


# --- caching -----------------------------------------------------------------


def test_projection_is_cached_until_the_log_changes(
    sample_archive: SampleArchive, author: Author
) -> None:
    service = ArchiveService(sample_archive.root)
    first = service.projection()
    assert service.projection() is first

    # An outside writer (the CLI, a git pull) appends to the log behind our back.
    create_node_from_content(
        open_index(sample_archive.root),
        "external",
        MediaType.TEXT_PLAIN,
        author.public_key,
        author.signing_key,
        None,
    )
    refreshed = service.projection()
    assert refreshed is not first
    assert len(refreshed.nodes) == 3


# --- integrity ---------------------------------------------------------------


def test_tampered_archive_raises_instead_of_returning_an_empty_graph(
    sample_archive: SampleArchive,
) -> None:
    tamper_last_signature(sample_archive.root)
    with pytest.raises(ArchiveIntegrityError, match="Data tampered or bad author!"):
        ArchiveService(sample_archive.root).projection()


def test_tampering_after_a_cached_read_is_detected(sample_archive: SampleArchive) -> None:
    service = ArchiveService(sample_archive.root)
    service.projection()
    tamper_last_signature(sample_archive.root)
    with pytest.raises(ArchiveIntegrityError):
        service.projection()


# --- writes ------------------------------------------------------------------


def test_write_returns_the_operation_result_and_refreshes(
    sample_archive: SampleArchive, author: Author
) -> None:
    service = ArchiveService(sample_archive.root)
    before = service.projection()
    node_id = service.write(
        lambda index: create_supersede_event(
            archive_idx=index,
            node_id=sample_archive.node_b,
            content="# Lemma B, revised",
            title="B2",
            media_type=MediaType.TEXT_MARKDOWN,
            public_key=author.public_key,
            signing_key=author.signing_key,
        )
    )
    after = service.projection()
    assert node_id == sample_archive.node_b
    assert after is not before
    node_b = after.nodes[sample_archive.node_b]
    assert (node_b.title, node_b.alias, node_b.revised) == ("B2", "e2", True)


def test_concurrent_writes_keep_the_chain_valid(
    sample_archive: SampleArchive, author: Author
) -> None:
    service = ArchiveService(sample_archive.root)
    errors: list[BaseException] = []

    def add_node(i: int) -> None:
        try:
            service.write(
                lambda index: create_node_from_content(
                    index,
                    f"concurrent {i}",
                    MediaType.TEXT_PLAIN,
                    author.public_key,
                    author.signing_key,
                    None,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=add_node, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    open_index(sample_archive.root)  # raises IntegrityError if the chain forked
    assert len(service.projection().nodes) == 2 + 8
