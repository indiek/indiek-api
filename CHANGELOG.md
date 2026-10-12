# Changelog

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
