# Changelog

## 0.13.0 — 2026-10-22

Follows `indiek` 1.1.0 and the format 3.1. 

### Added
- Edges without content (indiek format 3.1): `POST …/edges` accepts `content` omitted or
  `null`; `PUT …/edges/{id}` without `content` (or `null`) removes an edge's content, and a
  later `PUT` with text restores it. An empty string is still `422`.
- `EdgeOut.resource_type` in `GET …/graph`: `"managed"`, or `"none"` for an edge without
  content (whose `content`, `media_type` and `blob_hash` are `null`). The web UI ignores it
  for now (`src/lib/types.ts` not updated; WebUI out of scope).
- Tests: content-less edges (create, supersede both ways, events stamped `3.1`), edge
  `resource_type`, and a `local_file` `.pdf` node (detected as
  `application/pdf; profile=pdfa` by indiek 1.1.0; no API change was needed).

## 0.12.0 — 2026-10-21

### Changed
- `POST /api/workspaces` only registers IndieK archives (a folder holding
  `MANIFEST.json`, `events/` or `meta/events/`). A folder that is not an archive but
  has exactly one archive subfolder (e.g. a git repo with the archive in `kg/`)
  registers that subfolder; with several it answers `409 ambiguous_archive` plus a
  `candidates` list; with none `422 not_an_archive`. Nothing is written in either case
  (previously any existing folder was accepted as an empty archive).

### Added
- `POST /api/workspaces {path, create: true}` initialises a new archive (MANIFEST.json,
  SPEC.md via the SDK) in a folder that is empty apart from hidden entries such as
  `.git`; a missing folder is created when its parent exists (`404` "Parent folder
  does not exist" otherwise, nothing created), listing the configured author if `IK_AUTHOR_PRIVATE_KEY` is set;
  `409 folder_not_empty` otherwise.
- Error responses may carry extra fields (`IndieKAPIError.extra()`).

## 0.11.0 — 2026-10-21

### Changed
- Ported to the `indiek` SDK facade (`indiek.Archive`, `indiek.Author`): the API no
  longer imports the legacy `indiek.blob` / `create_*` / `set_tag` modules, which
  were removed upstream. Archives must use format `3.0`; any other version answers
  `409 archive_integrity`.
- Archives are opened at the SDK's default `incremental` verification level, backed
  by the per-machine verification cache (`IK_CACHE_DIR`, outside the archive).
- SDK errors are mapped by exception type instead of message substrings:
  `AuthorshipError` → 403 `forbidden`, `UnknownItemError` → 404 `item_not_found`,
  `IntegrityError` → 409 `archive_integrity`, other rule/validation errors → 422
  `archive_rule`.
- `PUT …/edges/{id}` with the ID of a node (and vice versa) now answers 404
  `item_not_found` (was 422): the ID does not name an item of that kind.
- `ArchiveService.write(author, operation)` passes an `indiek.Archive` to
  `operation`; `ArchiveService.create()` and `.index()` are gone (SDK writes return
  the new item's UUID). `Settings.author()` replaces `Settings.author_credentials()`.

### Tests
- Demo fixture rebuilt in format 3.0 by `tests/fixtures/build_demo_archive.sh`
  (drives the `ik` CLI from `tests/fixtures/demo_sources/`, throwaway key).
- Tests isolate the SDK verification cache per test (`IK_CACHE_DIR` under `tmp_path`).
- New `tests/test_errors.py` for the error mapping.

## 0.10.0 — 2026-10-12

### Changed (breaking)
- Code moved into the `indiek_api` package; run with `uv run python -m indiek_api`.
- All archive routes are scoped as `/api/workspaces/{workspace_id}/…`; the
  `network_id` query parameter is gone and `network_id` is now `workspace_id`.
- `GET …/graph` returns nodes, edges **and** tags in one payload (replaces `/api/tags`),
  with `e{n}`/`m{n}` aliases, tag kinds and a `revised` flag for superseded items.
- Write endpoints return the created item as `{id, alias}`; tag assignment returns
  the target's tags.
- Errors are `{"detail", "code"}` with deliberate status codes (404 / 403 / 409 / 422 / 503).

### Added
- Local-folder workspaces edited in place, persisted in `~/.indiek/workspaces.json`;
  `DELETE /api/workspaces/{id}` forgets a folder without touching its files.
- Settings via `INDIEK_API_*` variables; optional serving of the built web UI at `/`.
- Test suite (pytest + TestClient) on throwaway archives and a CLI-built demo archive.
- `uri` and `local_file` nodes via `POST …/nodes` (`resource_type`, default `managed`).
- `PUT …/edges/{id}` supersedes an edge's text (endpoints never change).
- `GET …/events`: base and meta event logs in log order (the `ik read` equivalent).
- `created_at` / `updated_at` on nodes, edges and tags; edges carry their text as `content`.

### Fixed
- The API could not be installed: it depended on `ik`, renamed `indiek` upstream.
- A tampered or corrupt archive was shown as an empty graph (and cached); it now
  answers `409 archive_integrity` with the library's message.
- Concurrent writes could fork the event chain; writes are serialised per workspace.
- The archive is re-verified when its event logs change on disk (CLI or `git pull`).
- `python main.py` listened on `0.0.0.0`; the server now binds `127.0.0.1` only.
- Tag creation accepted an empty description, which the CLI forbids.

### Removed
- URL and ZIP-upload workspaces and the `.workspaces/` cache folder.
- Dependencies `networkx`, `pillow`, `starlette` (direct), `python-multipart`.
- No GraphML export: the UI works from the live API, not from `ik export` files.
