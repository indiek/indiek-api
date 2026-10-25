"""DESKTOP:KEY-ONBOARDING (API side): the signing identity of the desktop app.

Owner decisions 2026-10-25: key in the OS keychain via `indiek.keys` (shared with `ik`);
IK_AUTHOR_PRIVATE_KEY wins; backups are password-encrypted files only and the plaintext seed
never leaves the backend; no usable keychain → clear error.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from indiek import Author, keys
from indiek.keys import KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE

from conftest import AUTH_HEADERS, api_client
from indiek_api.app import create_app
from indiek_api.settings import Settings

PASSWORD = "correct horse battery"


@pytest.fixture
def app_client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The API with NO key in its environment (a fresh desktop install)."""
    monkeypatch.delenv("IK_AUTHOR_PRIVATE_KEY", raising=False)
    with api_client(create_app(settings)) as client:
        yield client


def _stored(memory_keyring: Any) -> Author | None:
    seed = memory_keyring.store.get((KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT))
    return Author.from_seed_hex(seed) if seed else None


def _no_secret(response: Any, *authors: Author) -> None:
    for author in authors:
        assert author.seed_hex not in response.text


def test_fresh_install_has_no_identity(app_client: TestClient) -> None:
    response = app_client.get("/api/identity")
    assert response.status_code == 200
    assert response.json() == {"public_key": None, "source": "none", "keychain_available": True}


def test_generate_stores_in_the_keychain_and_signs(
    app_client: TestClient, memory_keyring: Any, tmp_path: Any
) -> None:
    response = app_client.post("/api/identity/generate", json={})
    assert response.status_code == 201, response.text
    stored = _stored(memory_keyring)
    assert stored is not None
    assert response.json() == {
        "public_key": stored.public_key_hex,
        "source": "keychain",
        "keychain_available": True,
    }
    _no_secret(response, stored)
    # Writes now succeed, signed by the keychain key.
    folder = tmp_path / "new"
    created = app_client.post("/api/workspaces", json={"path": str(folder), "create": True})
    assert created.status_code == 201, created.text
    workspace_id = created.json()["workspace_id"]
    node = app_client.post(f"/api/workspaces/{workspace_id}/nodes", json={"content": "hello"})
    assert node.status_code == 201, node.text
    graph = app_client.get(f"/api/workspaces/{workspace_id}/graph").json()
    assert graph["nodes"][0]["author"] == stored.public_key_hex


def test_generate_refuses_to_replace_unless_asked(
    app_client: TestClient, memory_keyring: Any
) -> None:
    first = app_client.post("/api/identity/generate", json={}).json()["public_key"]
    again = app_client.post("/api/identity/generate", json={})
    assert again.status_code == 409
    assert again.json()["code"] == "key_exists"
    replaced = app_client.post("/api/identity/generate", json={"replace": True})
    assert replaced.status_code == 201
    assert replaced.json()["public_key"] != first
    assert app_client.get("/api/identity").json()["public_key"] == replaced.json()["public_key"]


def test_env_wins_and_is_reported(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, memory_keyring: Any
) -> None:
    keys.store_in_keychain(Author.generate())
    env = Author.generate()
    monkeypatch.setenv("IK_AUTHOR_PRIVATE_KEY", env.seed_hex)
    with api_client(create_app(settings)) as client:
        body = client.get("/api/identity").json()
    assert body["source"] == "env" and body["public_key"] == env.public_key_hex


def test_import_a_hex_seed(app_client: TestClient, memory_keyring: Any) -> None:
    author = Author.generate()
    response = app_client.post("/api/identity/import", json={"seed_hex": author.seed_hex})
    assert response.status_code == 201, response.text
    assert response.json()["public_key"] == author.public_key_hex
    _no_secret(response, author)
    assert _stored(memory_keyring).seed_hex == author.seed_hex  # type: ignore[union-attr]


def test_import_a_malformed_seed(app_client: TestClient) -> None:
    response = app_client.post("/api/identity/import", json={"seed_hex": "zz"})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_key"


def test_import_needs_exactly_one_source(app_client: TestClient) -> None:
    author = Author.generate()
    backup = keys.export_backup(author, PASSWORD).decode()
    both = {"seed_hex": author.seed_hex, "backup": backup, "password": PASSWORD}
    for body in ({}, both, {"backup": backup}):
        response = app_client.post("/api/identity/import", json=body)
        assert response.status_code == 422, body
        assert response.json()["code"] == "invalid_key"
        assert author.seed_hex not in response.text and PASSWORD not in response.text


def test_backup_download_and_restore(app_client: TestClient, memory_keyring: Any) -> None:
    created = app_client.post("/api/identity/generate", json={}).json()
    original = _stored(memory_keyring)
    assert original is not None
    response = app_client.post("/api/identity/backup", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    disposition = response.headers["content-disposition"]
    assert f'filename="indiek-key-{created["public_key"][:8]}.ikkey"' in disposition
    assert response.headers["cache-control"] == "no-store"
    _no_secret(response, original)
    assert json.loads(response.content)["public_key"] == original.public_key_hex
    memory_keyring.store.clear()
    restored = app_client.post(
        "/api/identity/import",
        json={"backup": response.content.decode(), "password": PASSWORD},
    )
    assert restored.status_code == 201, restored.text
    assert _stored(memory_keyring).seed_hex == original.seed_hex  # type: ignore[union-attr]


def test_backup_errors(app_client: TestClient) -> None:
    nothing = app_client.post("/api/identity/backup", json={"password": PASSWORD})
    assert nothing.status_code == 503 and nothing.json()["code"] == "author_key_missing"
    app_client.post("/api/identity/generate", json={})
    short = app_client.post("/api/identity/backup", json={"password": "short"})
    assert short.status_code == 422


def test_restore_with_a_wrong_password(app_client: TestClient) -> None:
    backup = keys.export_backup(Author.generate(), PASSWORD).decode()
    response = app_client.post(
        "/api/identity/import", json={"backup": backup, "password": "not the password"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_backup"


def test_no_usable_keychain(app_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from keyring.backends import fail

    monkeypatch.setattr(keys, "_default_backend", fail.Keyring)
    body = app_client.get("/api/identity").json()
    assert body == {"public_key": None, "source": "none", "keychain_available": False}
    response = app_client.post("/api/identity/generate", json={})
    assert response.status_code == 503
    assert response.json()["code"] == "keychain_unavailable"
    assert "IK_AUTHOR_PRIVATE_KEY" in response.json()["detail"]


def test_the_keychain_is_read_once_then_cached(
    app_client: TestClient, memory_keyring: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_client.post("/api/identity/generate", json={})
    reads: list[str] = []
    original = memory_keyring.get_password

    def counting(service: str, username: str) -> str | None:
        reads.append(username)
        return original(service, username)

    monkeypatch.setattr(memory_keyring, "get_password", counting)
    for _ in range(3):
        app_client.get("/api/identity")
    assert reads == []  # served from the provider's cache, refreshed by identity routes


def test_identity_routes_need_the_token_and_the_write_header(settings: Settings) -> None:
    bare = TestClient(create_app(settings), base_url="http://127.0.0.1")
    assert bare.get("/api/identity").status_code == 401
    token_only = {"Authorization": AUTH_HEADERS["Authorization"]}
    assert bare.post("/api/identity/generate", json={}, headers=token_only).status_code == 403
