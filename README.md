# IndieK local API

The HTTP backend of the IndieK Desktop App (FOSS). It opens local archive folders
(typically git repositories) **in place**, verifies them with the
[`indiek`](https://pypi.org/project/indiek/) library, and exposes their current state
and the archive write operations to the IndieK web UI (`custom-graph-explorer`).

```
web UI (browser) ──HTTP──► indiek_api (this repo, 127.0.0.1 only) ──► indiek library ──► archive folder on disk
                                                                       (PyPI: indiek)        (git repo, edited in place)
```

The server always binds to `127.0.0.1`: it signs archive writes with your key,
so it must never be reachable from the network.

---

## 1. Prerequisites

| Tool | Notes |
| --- | --- |
| Python ≥ 3.14 | pinned in `.python-version`; `uv` installs it if missing |
| [`uv`](https://docs.astral.sh/uv/) | the **only** supported package/venv manager (no pip, no poetry) |
| [`indiek`](https://pypi.org/project/indiek/) | installed from PyPI (version pinned in `uv.lock`); nothing else to check out. To run against an **unreleased** local checkout of the library, overlay it per command without touching `pyproject.toml`: `uv run --with-editable ../public_archive pytest` (any path to the `indiek` source works). |

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
| `uv run python -m indiek_api`                | Run the server on 127.0.0.1, random port; prints the one-time launch URL and opens it in the browser (`--no-browser` to only print it). Docs at `/docs`, schema at `/openapi.json` |
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

The server prints `IndieK Desktop: open http://127.0.0.1:<port>/api/session?token=…` and opens
it: that single-use link logs the browser in (section 7, "Access control"). Restarting the server
invalidates the session: use the new link.

The server does not auto-reload: restart it after code changes.

---

## 4. Configuration

All settings are environment variables, read in `indiek_api/settings.py`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `IK_AUTHOR_PRIVATE_KEY` | — | 64-hex Ed25519 seed that signs writes (same variable as the `ik` CLI, see the [`indiek` README](https://pypi.org/project/indiek/)). **Overrides the OS-keychain key** created or imported in the app (`/api/identity`). Without either, reads work and writes return `503 author_key_missing`. |
| `INDIEK_API_ENV_FILE` | `fastAPI/.env` | Dotenv file loaded at startup; real environment variables win. |
| `INDIEK_API_REGISTRY` | `~/.indiek/workspaces.json` | Registered archive folders (JSON). |
| `INDIEK_API_PORT` | random free port | Fixed port (host is always `127.0.0.1`). |
| `INDIEK_API_TOKEN` | random per launch | Fixed API token, **development and tests only** (≥ 16 characters); stays reusable, so `…/api/session?token=<it>` can be reopened. |
| `INDIEK_API_OPEN_BROWSER` | `1` | `0` = only print the launch URL (same as `--no-browser`). |
| `INDIEK_API_CSP_REPORT_ONLY` | `0` | `1` = send the CSP as `Content-Security-Policy-Report-Only` (debugging: violations are logged in the browser console, nothing is blocked). |
| `INDIEK_API_STATIC_DIR` | — | Built web UI to serve at `/` (see section 5). |
| `INDIEK_API_CORS_ORIGINS` | — | Comma-separated origins; only needed when a browser calls the API from another origin. |

Typical setups:

- **Your key**: put `IK_AUTHOR_PRIVATE_KEY=…` in `fastAPI/.env` (git-ignored; never commit it).
- **Experiments without touching your real registry**:
  `INDIEK_API_REGISTRY=/tmp/ik-registry.json INDIEK_API_PORT=8765 uv run python -m indiek_api`.
- **Browser testing with a stable URL**: add `INDIEK_API_PORT=8765 INDIEK_API_TOKEN=<16+ chars>`
  and open `http://127.0.0.1:8765/api/session?token=<the token>` once per browser.

### Launched by the desktop shell (DESKTOP:REPO-SPLIT-3)

The Tauri shell starts the server as a child process with two flags (both usable without Tauri):

| Flag | Effect |
| --- | --- |
| `--handshake` | Print exactly one JSON line on stdout, `{"indiek_api": "<version>", "port": <port>, "launch_url": "<url>"}`, and never open a browser. All logs (uvicorn's access log included) go to stderr, so the one-time launch token travels only through the pipe to the parent. The shell opens `launch_url` in its webview. |
| `--exit-on-stdin-eof` | Shut down when stdin is closed: the server never outlives a crashed parent. |

Try it: `uv run python -m indiek_api --handshake --exit-on-stdin-eof` (Ctrl-D stops it).

### Embedding the API as a library (DESKTOP:REUSE-SEAMS)

`create_app(settings=None, *, workspaces=None, signer=None)` accepts any object matching the
protocols of `indiek_api/providers.py`: `WorkspaceProvider` (`list`, `register`, `remove`,
`service`; default `WorkspaceRegistry`) and `Signer` (`author`, `author_or_none`; default
`KeyProvider`). The `/api/identity/*` routes are mounted only when the signer is also a
`KeyManager` (`status`, `store`, `backup`, `read_backup`), as `KeyProvider` is.

---

## 5. Working with the frontend

- **Frontend development**: run this server as usual and `npm run dev` in
  `webUI/custom-graph-explorer`. Next proxies `/api/*` to `INDIEK_API_URL`
  (default `http://127.0.0.1:8000`), so the browser sees one origin — **no CORS setting
  needed**. Start this server with `INDIEK_API_PORT=8000 INDIEK_API_TOKEN=<16+ chars>`, then
  open `http://localhost:3000/api/session?token=<the token>` once: the cookie lands on
  `localhost:3000` and the proxy forwards it.
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
  app.py               FastAPI app factory, all routes, static-files mount, CORS; main() = launcher
  security.py          access control: loopback Host, token (Bearer/cookie), write header, CSP
  identity.py          KeyProvider: the default signer (env key, else OS-keychain key via indiek.keys)
  providers.py         reuse seams: WorkspaceProvider, Signer, KeyManager protocols for create_app
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
  test_security.py     access control, launch link, CSP, launcher
  test_identity.py     signing identity routes (in-memory keychain, never the real one)
  test_write_lock.py   indiek's cross-process write lock seen through the API
  test_desktop_seams.py  injected providers, --handshake, --exit-on-stdin-eof (real subprocess)
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
Full request/response schemas: `http://127.0.0.1:<port>/docs`.

### Access control (DESKTOP:LOCALHOST-TOKEN)

Design, threats and owner decisions: `project_management/Design.md`, cybersecurity section.

- Only loopback `Host` headers are served (`127.0.0.1`, `localhost`, `[::1]`, any port):
  defeats DNS rebinding. Applies to every path.
- Every `/api/*` route needs the per-launch token: `Authorization: Bearer <token>` (for the
  future Tauri shell) or the session cookie `ik_session_<port>` (HttpOnly, SameSite=Strict).
  `GET /api/session?token=<launch token>` sets that cookie and redirects to `/`; the launch
  token works **once** (unless fixed with `INDIEK_API_TOKEN`). `/health` and the UI files are open.
- `POST`/`PUT`/`PATCH`/`DELETE` on `/api/*` also need `X-IndieK-Request: 1`: a cross-origin page
  would need a CORS preflight, which is never granted, so other pages cannot write.
- Every response carries a strict `Content-Security-Policy`: `script-src` lists the SHA-256 of
  each inline script of `INDIEK_API_STATIC_DIR`'s HTML, computed at startup (**restart after
  `npm run build`**); no `'unsafe-inline'`/`'unsafe-eval'` for scripts. Blob responses get a
  sandboxed policy (`default-src 'none'; …; sandbox`), plus `X-Content-Type-Options: nosniff`.

| Method & path | Body | Response |
| --- | --- | --- |
| `GET /health` | — | `{"status": "ok", "api_version": "<version>"}` (no token needed) |
| `GET /api/session?token=…` | — | `303 → /` + session cookie; `401` if the launch token is wrong or used |
| `GET /api/identity` | — | `{public_key, source: "env" \| "keychain" \| "none", keychain_available}` — never the secret |
| `POST /api/identity/generate` | `{replace?: false}` | `201`, same shape; new key stored in the OS keychain |
| `POST /api/identity/import` | `{seed_hex}` \| `{backup, password}`, plus `replace?` | `201`, same shape |
| `POST /api/identity/backup` | `{password}` (≥ 12 characters) | the **encrypted** backup file, `attachment; filename="indiek-key-<8 hex>.ikkey"`, `no-store` (≈ 4 s: Argon2id) |
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
| 409 | `archive_locked` | another process (e.g. `ik`) held the archive's write lock `.ik.lock` for more than 10 s; retry |
| 403 | `forbidden` | assigning another author's tag |
| 422 | `archive_rule` | the library refused the operation (e.g. tag kind ≠ target kind) |
| 422 | *(none)* | FastAPI request validation; `detail` is a list of issues |
| 503 | `author_key_missing` | a write without `IK_AUTHOR_PRIVATE_KEY` |
| 400 | `invalid_host` | `Host` header is not a loopback name |
| 401 | `unauthorized` | `/api/*` without a valid token (Bearer or session cookie), or a wrong/used launch link |
| 403 | `missing_request_header` | a write without `X-IndieK-Request: 1` |
| 409 | `key_exists` | generate/import while the keychain already holds a key, without `replace: true` |
| 422 | `invalid_key` | malformed `seed_hex`, or not exactly one of `seed_hex` / `backup` (never echoes the key) |
| 422 | `invalid_backup` | not a key backup, wrong password, or a damaged file |
| 422 | `weak_password` | backup password shorter than 12 characters |
| 503 | `keychain_unavailable` | no usable OS keychain: use `IK_AUTHOR_PRIVATE_KEY` |

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

For the library side (`indiek`), learn the API from the [`indiek` README](https://pypi.org/project/indiek/); treat the CLI
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
| `settings`, `client` | test settings (fixed token `TEST_TOKEN`) and an HTTP test client that calls the API as the UI does (`127.0.0.1`, Bearer token, write header; see `api_client()`) |
| `workspace` | `sample_archive` registered in place as a workspace |

Helpers: `open_index(root)` (open, verify and index an archive exactly as the CLI does),
`build_sample_archive`, `tamper_last_signature` (to test `409 archive_integrity`).

---

## 10. Conventions

- Python 3.10+ style with strict type hints everywhere; PEP 8 via ruff (line length 100).
- `uv` only.
- Keep routes thin; logic lives in `ArchiveService`; every error has a stable `code`.
- Never bind to anything but `127.0.0.1`; never log or return secrets (the launch URL printed at
  startup is the one deliberate exception: it is single-use).
- Record significant changes in `CHANGELOG.md`.

---

## 11. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `503 author_key_missing` on writes | Create or import a key in the app (Author key dialog), or set `IK_AUTHOR_PRIVATE_KEY` (env or `fastAPI/.env`) and restart |
| The app shows `source: env` and ignores a key created in it | `IK_AUTHOR_PRIVATE_KEY` is set (env or `fastAPI/.env`) and wins: remove it to use the keychain key |
| `409 archive_integrity` | The archive on disk is inconsistent (e.g. a manual edit or bad merge); check it with the `ik` CLI |
| `409 archive_locked` | Another process is writing to the archive (a long `ik ingest`/`ik seal`, or a stuck process); retry, or stop it |
| `404 archive_not_found` when registering | The folder does not exist. Paths are `~`-expanded and resolved relative to the server's working directory, so prefer absolute paths |
| `Address already in use` | Another server on the fixed `INDIEK_API_PORT`: stop it, or unset the variable (random port) |
| `401 unauthorized` everywhere | Open the launch link the server printed (each restart prints a new one; a link works once) |
| `403 missing_request_header` | A client other than the UI: send `X-IndieK-Request: 1` on writes |
| UI blank, CSP errors in the browser console | The UI was rebuilt after the server started: restart it (hashes are computed at startup); `INDIEK_API_CSP_REPORT_ONLY=1` to debug |
| Frontend says `network_error` | Server not running, or frontend's `INDIEK_API_URL` points to another port |
| `uv sync` cannot find `indiek` | No network access to PyPI, or `uv.lock` pins an `indiek` version not published yet (section 1) |

## 12. Support IndieK

[![GitHub Sponsors](https://img.shields.io/badge/GitHub_Sponsors-indiek-bf3989?logo=githubsponsors)](https://github.com/sponsors/indiek)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-support-FF5E5B?logo=ko-fi)](https://ko-fi.com/indiek_sponsoring)

IndieK is free and open-source software. If it's useful to you, you can support its development:

- [GitHub Sponsors](https://github.com/sponsors/indiek), for developers with a GitHub account (0% platform fees)
- [Ko-fi](https://ko-fi.com/indiek_sponsoring), one-time or monthly, no account needed

All donations strictly fund the open-source core of IndieK and its public infrastructure.
