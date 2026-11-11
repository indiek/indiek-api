# Changelog

## Unreleased

The owner releases `indiek-api` **1.0.0** on PyPI together with the first packaged desktop app.

## 0.17.1 — 2026-11-11

### Fixed
- `indiek_api.__version__` (reported by `/health` as `api_version` and by the `--handshake`
  line) was hardcoded and still said `0.13.0`; it now comes from the installed package metadata
  (single source: `pyproject.toml`). Regression test compares it with `pyproject.toml`.
- Project URLs (`Repository`, `Changelog`) now point to `https://github.com/indiek/indiek-api`;
  the 0.17.0 PyPI page links to the non-existent `indiek/fastAPI` repository.

## 0.17.0 — 2026-11-01

### Changed
- `indiek` now comes from PyPI (`indiek[keychain]>=1.3.0`, pinned in `uv.lock`) instead of the
  sibling `../public_archive` path source, so a fresh clone of this repo installs on its own.
  Unreleased library work: `uv run --with-editable ../public_archive …` (README section 1).
- README: links to the `indiek` library point to PyPI instead of workspace-relative paths.

### Added
- Desktop shell seams (DESKTOP:REPO-SPLIT-3, owner decisions 2026-10-30; the Tauri webview opens
  the UI served here, so the frontend needs no change):
  - `--handshake`: one JSON line `{"indiek_api", "port", "launch_url"}` on stdout, no browser;
    all logs, including uvicorn's access log, go to stderr.
  - `--exit-on-stdin-eof`: the server stops when its parent closes stdin.
  - `GET /health` also returns `api_version`.
- Reuse seams (DESKTOP:REUSE-SEAMS): new `indiek_api/providers.py` with the `WorkspaceProvider`,
  `Signer` and `KeyManager` protocols; `create_app(settings, *, workspaces=None, signer=None)`.
  Defaults unchanged (`WorkspaceRegistry`, `KeyProvider`). The `/api/identity/*` routes are
  mounted only when the signer is a `KeyManager` (moved into `_add_identity_routes`).
- Tests: `tests/test_desktop_seams.py` (7, one runs the real server as a subprocess).
  Suite: 138 passed.
- Packaging metadata in `pyproject.toml`: hatchling build backend, Apache-2.0 license + `LICENSE`
  file, classifiers, project URLs (stub `github.com/indiek/fastAPI`, not on GitHub yet), console
  script `indiek-api` (same as `python -m indiek_api`). The sdist uses an explicit allow-list so
  local files such as `.env*` are never shipped.
- CI: `.github/workflows/checks.yml` (ruff + pytest on every push and pull request) and
  `.github/workflows/release-and-publish.yml` (on a version tag pushed by the owner: lint, test,
  `uv build`, GitHub Release, PyPI trusted publishing).
- README: "Support IndieK" section with GitHub Sponsors (`indiek` org) and Ko-fi (`indiek_sponsoring`) links (FOSSDOCS:DONATION-INSTRUCTIONS).

## 0.16.0 — 2026-10-25

Signing identity for the desktop app (DESKTOP:KEY-ONBOARDING); requires `indiek[keychain]>=1.3.0`.

### Added
- `indiek_api/identity.py`: `KeyProvider`, the API's signing seam (DESKTOP:REUSE-SEAMS):
  `IK_AUTHOR_PRIVATE_KEY` (re-read on every call) wins, else the OS-keychain key via
  `indiek.keys`, read once and cached (no keychain prompt per write), refreshed after every
  identity change. All write routes sign through it.
- Routes: `GET /api/identity`, `POST /api/identity/generate`, `POST /api/identity/import`
  (`seed_hex` or encrypted `backup` + `password`), `POST /api/identity/backup` (encrypted file
  download, `Cache-Control: no-store`). The plaintext seed is never returned; the "exactly one
  source" check lives in the route so validation errors never echo a secret.
- Error codes: `409 key_exists`, `422 invalid_key`, `422 invalid_backup`, `422 weak_password`,
  `503 keychain_unavailable`.
- Tests: `tests/test_identity.py` (13); autouse in-memory keychain + minimal Argon2id cost in
  `conftest.py`. 130 passed.

### Changed
- `Settings.author()` / `author_or_none()` removed (replaced by `KeyProvider`).
- `503 author_key_missing` message now points to the app's Author key dialog.

## 0.15.0 — 2026-10-25

Access control for the local API (DESKTOP:LOCALHOST-TOKEN). Design, threats and owner decisions:
`project_management/Design.md`, cybersecurity section.

### Added
- `indiek_api/security.py`: `AccessControlMiddleware` (pure ASGI). Loopback `Host` only
  (`400 invalid_host`); per-launch token on every `/api/*` route, as `Authorization: Bearer` or
  the HttpOnly SameSite=Strict cookie `ik_session_<port>` (`401 unauthorized`); writes also need
  `X-IndieK-Request: 1` (`403 missing_request_header`). Tokens compared with `hmac.compare_digest`.
- `GET /api/session?token=…`: single-use launch token → session cookie, `303 → /`
  (`Cache-Control: no-store`, `Referrer-Policy: no-referrer`).
- Strict `Content-Security-Policy` on every response, `script-src 'self'` + the SHA-256 of each
  inline script of the static UI computed at startup (11 in today's build); sandboxed policy for
  blobs; `X-Content-Type-Options: nosniff`. `INDIEK_API_CSP_REPORT_ONLY=1` for debugging.
- Launcher: `python -m indiek_api` binds a random free port, prints the launch URL and opens it
  in the default browser; `--no-browser` / `INDIEK_API_OPEN_BROWSER=0` disable the auto-open.
- `INDIEK_API_TOKEN`: fixed, reusable token for development and tests (≥ 16 characters).
- Tests: `tests/test_security.py` (26, incl. a check of the real `next build` output when
  present); the shared client now authenticates like the UI (`api_client()`, `TEST_TOKEN`). 117 passed.

### Changed
- Default port: random (was 8000); `INDIEK_API_PORT` still fixes it.
- **Breaking for other clients**: every `/api/*` call needs the token; writes need the header.

## 0.14.0 — 2026-10-25

Follows `indiek` 1.2.0 (SDK2:WRITE-LOCK); now requires `indiek>=1.2.0`.

### Added
- `409 archive_locked`: the SDK's new `ArchiveLockedError` (another process, e.g. the `ik`
  CLI, held `<archive>/.ik.lock` for more than 10 s) is mapped by type
  (`ArchiveLockedAPIError`). It used to fall through to `422 archive_rule`.
- Writes from the API and from `ik` on the same archive no longer fork the log: the SDK
  serialises them across processes (the in-process `threading.RLock` only covered the API's
  own threads). No route or service change was needed.
- Tests: mapping case in `tests/test_errors.py`; new `tests/test_write_lock.py` (a write
  while another process holds the lock → 409, archive unchanged, reads still work, next
  write succeeds). 91 passed.

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
