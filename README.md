# IndieK local API

The HTTP backend of the IndieK Desktop App. It opens local archive folders
(typically git repositories) **in place**, verifies them with the
[`indiek`](../public_archive/README.md) library, and exposes their current state
and the archive write operations to the web UI.

## Run

```bash
uv sync
uv run python -m indiek_api        # http://127.0.0.1:8000, API docs at /docs
```

The server always binds to `127.0.0.1`: it signs archive writes with your key,
so it must never be reachable from the network.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `IK_AUTHOR_PRIVATE_KEY` | — | 64-hex Ed25519 seed that signs writes (same as the `ik` CLI). Reads work without it; writes return `503`. |
| `INDIEK_API_ENV_FILE` | `fastAPI/.env` | Dotenv file loaded at startup; real environment variables win. |
| `INDIEK_API_REGISTRY` | `~/.indiek/workspaces.json` | Registered archive folders. |
| `INDIEK_API_PORT` | `8000` | Port. |
| `INDIEK_API_STATIC_DIR` | — | Built web UI to serve at `/`. |
| `INDIEK_API_CORS_ORIGINS` | — | Comma-separated origins; only needed when the UI runs on another port. |

## Routes

All archive routes live under `/api/workspaces/{workspace_id}`. Item references
accept a UUID or an `e{n}` alias; tag references a UUID or an `m{n}` alias.

| Method & path | Purpose |
| --- | --- |
| `GET /health` | Liveness. |
| `GET/POST /api/workspaces`, `DELETE /api/workspaces/{id}` | List, register (`{"path": …}`), forget a folder (files are never touched). |
| `GET …/graph` | Current nodes, edges and tags (supersedes applied, aliases, tag kinds). |
| `GET …/blobs/{sha256}` | Blob bytes with their media type. |
| `GET …/events` | Base and meta event logs in log order (like `ik read`). |
| `POST …/nodes`, `PUT …/nodes/{id}` | Create a `managed` / `uri` / `local_file` node; supersede a node's text. |
| `POST …/edges`, `PUT …/edges/{id}` | Create an edge; supersede its text. |
| `POST …/tags`, `POST …/tags/{tag}/assignments` | Create a node/edge tag; assign it. |

Errors are `{"detail": …, "code": …}`: `404` unknown workspace/item/blob,
`409 archive_integrity` (broken chain, bad signature, missing blob),
`403 forbidden` (another author's tag), `422 archive_rule`, `503 author_key_missing`.

## Test

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Tests are hermetic: throwaway archives and keys under `tmp_path`, plus a copy of
`tests/fixtures/demo_archive` (built with the `ik` CLI README walkthrough).
