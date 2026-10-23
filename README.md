# IndieK local API

The HTTP backend of the IndieK Desktop App (FOSS). It opens local archive folders
(typically git repositories) **in place**, verifies them with the
[`indiek`](../public_archive/README.md) library, and exposes their current state
and the archive write operations to the web UI in
[`../webUI/custom-graph-explorer`](../webUI/custom-graph-explorer/README.md).

```
web UI (browser) ──HTTP──► indiek_api (this repo, 127.0.0.1 only) ──► indiek library ──► archive folder on disk
                                                                       (../public_archive)   (git repo, edited in place)
```

The server always binds to `127.0.0.1`: it signs archive writes with your key,
so it must never be reachable from the network.

---

## 1. Prerequisites

| Tool | Notes |
| --- | --- |
| Python ≥ 3.14 | pinned in `.python-version`; `uv` installs it if missing |
| [`uv`](https://docs.astral.sh/uv/) | the **only** supported package/venv manager (no pip, no poetry) |
| `../public_archive` | the `indiek` library is installed from this sibling folder as an **editable** path dependency (`[tool.uv.sources]` in `pyproject.toml`). Keep the `indiek/` workspace layout: `fastAPI/` and `public_archive/` side by side. Library changes are picked up without reinstalling. |

`fastAPI/` is its own git repository.

---

## 2. Setup

```bash
cd fastAPI
uv sync                 # creates .venv/ and installs runtime + dev dependencies from uv.lock
```

Dependency changes: `uv add <pkg>` / `uv add --dev <pkg>` / `uv remove <pkg>` (updates
`pyproject.toml` and `uv.lock`; commit both). Never edit `uv.lock` by hand.

---

## 3. Daily commands

| Command                                      | What it does                                                                                     |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `uv run python -m indiek_api`                | Run the server on http://127.0.0.1:8000 (interactive docs at `/docs`, schema at `/openapi.json`) |
| `uv run pytest`                              | Full test suite                                                                                  |
| `uv run pytest tests/test_api.py`            | One file                                                                                         |
| `uv run pytest tests/test_api.py::test_name` | One test                                                                                         |
| `uv run pytest -k supersede -x`              | Tests matching a keyword, stop at first failure                                                  |
| `uv run ruff check .`                        | Lint (`--fix` to auto-fix)                                                                       |
| `uv run ruff format .`                       | Format (`--check` to only verify)                                                                |

Before committing: `uv run ruff check . && uv run ruff format --check . && uv run pytest`.

To launch the UI with NextJS already built:
```{bash}
export INDIEK_API_ENV_FILE=".env" && INDIEK_API_STATIC_DIR="/Users/adrianradillo/prog/indiek/webUI/custom-graph-explorer/out" && uv run python -m indiek_api
```

The server does not auto-reload: restart it after code changes.

---

## 4. Configuration

All settings are environment variables, read in `indiek_api/settings.py`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `IK_AUTHOR_PRIVATE_KEY` | — | 64-hex Ed25519 seed that signs writes (same variable as the `ik` CLI, see `public_archive/README.md`). Reads work without it; writes return `503 author_key_missing`. |
| `INDIEK_API_ENV_FILE` | `fastAPI/.env` | Dotenv file loaded at startup; real environment variables win. |
| `INDIEK_API_REGISTRY` | `~/.indiek/workspaces.json` | Registered archive folders (JSON). |
| `INDIEK_API_PORT` | `8000` | Port (host is always `127.0.0.1`). |
| `INDIEK_API_STATIC_DIR` | — | Built web UI to serve at `/` (see section 5). |
| `INDIEK_API_CORS_ORIGINS` | — | Comma-separated origins; only needed when a browser calls the API from another origin. |

Typical setups:

- **Your key**: put `IK_AUTHOR_PRIVATE_KEY=…` in `fastAPI/.env` (git-ignored; never commit it).
- **Experiments without touching your real registry**:
  `INDIEK_API_REGISTRY=/tmp/ik-registry.json INDIEK_API_PORT=8765 uv run python -m indiek_api`.

---

## 5. Working with the frontend

- **Frontend development**: run this server as usual and `npm run dev` in
  `webUI/custom-graph-explorer`. Next proxies `/api/*` to `127.0.0.1:8000`, so the browser
  sees one origin — **no CORS setting needed**. If you change the port, start the frontend with
  `INDIEK_API_URL=http://127.0.0.1:<port> npm run dev`.
- **Desktop build (one process)**: build the UI, then point this server at it:
  ```bash
  (cd ../webUI/custom-graph-explorer && npm run build)
  INDIEK_API_STATIC_DIR="$(cd ../webUI/custom-graph-explorer/out && pwd)" uv run python -m indiek_api
  ```
  `/api/*` and `/health` are the API; every other path is a file from `out/`.
- **Changing the API contract**: the frontend mirrors it by hand in
  `webUI/custom-graph-explorer/src/lib/types.ts` and `src/lib/api.ts`. Update both sides (and
  their tests) together.

---

## 6. Code layout

```
indiek_api/
  __main__.py          entry point: `python -m indiek_api` → uvicorn on 127.0.0.1:<port>
  app.py               FastAPI app factory, all routes, static-files mount, CORS
  schemas.py           Pydantic request/response models (the wire contract)
  archive_service.py   ArchiveService: verified, cached, write-serialised access to ONE archive
  workspaces.py        registry of archive folders (workspace = pointer to a folder)
  errors.py            domain errors and their mapping to {"detail", "code"} responses
  settings.py          environment/dotenv configuration
tests/
  conftest.py          hermetic fixtures (see section 9)
  test_api.py          HTTP contract of every route
  test_archive_service.py   projection, caching, integrity, write serialisation
  test_cli_parity.py   results match the `ik` CLI (resource types, edge supersedes, timestamps, events)
  fixtures/demo_archive/    a real archive built with the `ik` CLI README walkthrough
CHANGELOG.md, pyproject.toml, uv.lock, .python-version
```

How a request flows: `app.py` route → looks up the workspace in `workspaces.py` → gets the
folder's `ArchiveService` → which uses the `indiek` library → result serialised with a
`schemas.py` model. Errors raised anywhere are domain errors from `errors.py`, turned into
JSON by the exception handlers.

`ArchiveService` behaviour worth knowing:

- The archive is **re-verified** (chain, signatures, blobs) whenever its event logs change on
  disk, whoever changed them: this API, the `ik` CLI or a `git pull`. Otherwise the cached
  projection is reused.
- A blob altered on disk *without* any log change is only caught at the next full verification
  (after the next log change).
- Writes are serialised by a lock **inside this process only**; it does not guard against the
  `ik` CLI appending to the same archive at the same instant.

---

## 7. Routes

All archive routes live under `/api/workspaces/{workspace_id}`. Item references
accept a UUID or an `e{n}` alias; tag references a UUID or an `m{n}` alias.
Full request/response schemas: http://127.0.0.1:8000/docs.

| Method & path | Body | Response |
| --- | --- | --- |
| `GET /health` | — | `{"status": "ok"}` |
| `GET /api/workspaces` | — | `[{workspace_id, name, path, created_at}]` |
| `POST /api/workspaces` | `{path}` | `201` new / `200` already registered, same shape |
| `DELETE /api/workspaces/{id}` | — | `204`; forgets the folder, files are never touched |
| `GET …/graph` | — | `{head, meta_head, nodes[], edges[], tags[]}` — supersedes applied, aliases, tag kinds; each edge has `resource_type` (`"managed"`, or `"none"` without content) |
| `GET …/blobs/{sha256}` | — | blob bytes with their media type (hex, prefix allowed) |
| `GET …/events` | — | base + meta event logs in log order (like `ik read`) |
| `POST …/nodes` | `{resource_type?: "managed", content, title?, media_type?}` \| `{resource_type: "uri", uri, title?, media_type?}` \| `{resource_type: "local_file", path, title?}` | `201 {id, alias}` |
| `PUT …/nodes/{id}` | `{content, title?}` (stored as Markdown) | `{id, alias}` |
| `POST …/edges` | `{source, target, content?}` — omit `content` (or `null`) for an edge without content | `201 {id, alias}` |
| `PUT …/edges/{id}` | `{content?}` — omit or `null` to remove the edge's content | `{id, alias}` |
| `POST …/tags` | `{name, description, kind: "node" \| "edge"}` | `201 {id, alias}` |
| `POST …/tags/{tag}/assignments` | `{target}` | `201 {target, tags[]}` — tag kind must match the target; only the tag's author may assign it |

### Errors

Every error is `{"detail": <message>, "code": <stable slug>}`; clients branch on `code`.

| Status | `code` | When |
| --- | --- | --- |
| 404 | `workspace_not_found`, `item_not_found`, `blob_not_found`, `archive_not_found` | unknown workspace/id/alias/hash, or the registered folder does not exist |
| 409 | `archive_integrity` | broken chain, bad signature, missing blob |
| 403 | `forbidden` | assigning another author's tag |
| 422 | `archive_rule` | the library refused the operation (e.g. tag kind ≠ target kind) |
| 422 | *(none)* | FastAPI request validation; `detail` is a list of issues |
| 503 | `author_key_missing` | a write without `IK_AUTHOR_PRIVATE_KEY` |

---

## 8. Recipe: adding a route

1. **Contract**: add request/response models to `schemas.py`.
2. **Test first**: add the HTTP test to `tests/test_api.py` (and to `test_cli_parity.py` if the
   CLI has an equivalent command, to prove identical results).
3. **Logic**: implement it as an `ArchiveService` method (with a unit test in
   `test_archive_service.py`) — routes stay thin.
4. **Route**: add it in `app.py`, mapping inputs to the service and the result to the schema.
5. **Errors**: raise existing domain errors; if a new failure kind is needed, add it in
   `errors.py` with its own stable `code`.
6. **Frontend**: mirror the types/function in `webUI/custom-graph-explorer/src/lib/`.
7. **Docs**: this README's route table and `CHANGELOG.md`; bump `version` in `pyproject.toml`
   for releases.

For the library side (`indiek`), learn the API from `public_archive/README.md`; treat the CLI
as stable.

---

## 9. Testing

Tests are hermetic: nothing reads a dotenv file, your real registry or your real archives.

Main fixtures in `tests/conftest.py`:

| Fixture | Gives you |
| --- | --- |
| `author` | the API's configured author; its seed is exported as `IK_AUTHOR_PRIVATE_KEY` |
| `other_author` | a second author (e.g. for `403 forbidden` cases) |
| `sample_archive` | throwaway archive under `tmp_path`: two nodes, one edge, one node tag, one assignment |
| `demo_archive` | private, mutable copy of `tests/fixtures/demo_archive` (e1–e10, m1–m6; e1 node and e8 edge superseded; e5 uri, e6 local_file; m1 node tag "theorem", m2 edge tag "proof") |
| `settings`, `client` | test settings and an HTTP test client for the app |
| `workspace` | `sample_archive` registered in place as a workspace |

Helpers: `open_index(root)` (open, verify and index an archive exactly as the CLI does),
`build_sample_archive`, `tamper_last_signature` (to test `409 archive_integrity`).

---

## 10. Conventions

- Python 3.10+ style with strict type hints everywhere; PEP 8 via ruff (line length 100).
- `uv` only.
- Keep routes thin; logic lives in `ArchiveService`; every error has a stable `code`.
- Never bind to anything but `127.0.0.1`; never log or return secrets.
- Record significant changes in `CHANGELOG.md`.

---

## 11. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `503 author_key_missing` on writes | Set `IK_AUTHOR_PRIVATE_KEY` (env or `fastAPI/.env`) and restart |
| `409 archive_integrity` | The archive on disk is inconsistent (e.g. a manual edit or bad merge); check it with the `ik` CLI |
| `404 archive_not_found` when registering | The folder does not exist. Paths are `~`-expanded and resolved relative to the server's working directory, so prefer absolute paths |
| `Address already in use` | Another server on 8000: stop it or set `INDIEK_API_PORT` |
| Frontend says `network_error` | Server not running, or frontend's `INDIEK_API_URL` points to another port |
| `uv sync` cannot find `indiek` | `../public_archive` is missing or moved (section 1) |
