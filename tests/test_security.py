"""DESKTOP:LOCALHOST-TOKEN: access control of the local API.

Design and owner decisions 2026-10-25: project_management/Design.md, cybersecurity section.
Hybrid token (Bearer header for the future Tauri shell, single-use launch token → HttpOnly
SameSite=Strict cookie for browser mode), loopback Host only, custom header on writes, token on
every /api/* route, strict Content-Security-Policy with startup-computed inline-script hashes.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from indiek_api import app as app_module
from indiek_api.app import create_app
from indiek_api.security import (
    REQUEST_HEADER,
    AccessTokens,
    build_csp,
    cookie_name,
    inline_script_hashes,
)
from indiek_api.settings import Settings

from conftest import TEST_TOKEN, Workspace

BASE = "http://127.0.0.1"
WRITE_HEADER = {REQUEST_HEADER: "1"}


def _sha(text: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    """A tiny stand-in for the Next static export, with inline scripts."""
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    (out / "index.html").write_text(
        '<html><head><script src="/_next/app.js"></script>'
        "<script>self.__next_f=self.__next_f||[]</script></head>"
        '<body><script id="d">self.__next_f.push([1,"x"])</script></body></html>',
        encoding="utf-8",
    )
    (out / "sub" / "index.html").write_text(
        "<html><script async>\nconsole.log('sub')\n</script></html>", encoding="utf-8"
    )
    return out


@pytest.fixture
def bare(settings: Settings) -> TestClient:
    """A client with no credentials at all (an outside caller)."""
    return TestClient(create_app(settings), base_url=BASE)


# -- tokens -----------------------------------------------------------------


def test_tokens_are_random_and_the_launch_token_is_single_use() -> None:
    first, second = AccessTokens(), AccessTokens()
    assert first.session != second.session and len(first.session) >= 40
    assert first.launch is not None and first.launch != first.session
    assert first.check_session(first.session)
    assert not first.check_session(second.session)
    assert not first.check_session("")
    launch = first.launch
    assert first.redeem_launch(launch)
    assert not first.redeem_launch(launch)


def test_a_fixed_token_is_reusable_for_development() -> None:
    tokens = AccessTokens(fixed=TEST_TOKEN)
    assert tokens.session == TEST_TOKEN
    assert tokens.redeem_launch(TEST_TOKEN) and tokens.redeem_launch(TEST_TOKEN)


def test_a_short_fixed_token_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INDIEK_API_TOKEN", "short")
    with pytest.raises(ValueError, match="INDIEK_API_TOKEN"):
        Settings.from_env()


def test_cookie_name_includes_the_port() -> None:
    assert cookie_name(51234) == "ik_session_51234"
    assert cookie_name(None) == "ik_session"


# -- who may call -------------------------------------------------------------


def test_api_routes_need_a_token(bare: TestClient) -> None:
    response = bare.get("/api/workspaces")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_wrong_token_is_refused(bare: TestClient) -> None:
    response = bare.get("/api/workspaces", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_a_bearer_token_opens_reads(bare: TestClient) -> None:
    response = bare.get("/api/workspaces", headers={"Authorization": f"Bearer {TEST_TOKEN}"})
    assert response.status_code == 200


def test_health_stays_open(bare: TestClient) -> None:
    assert bare.get("/health").status_code == 200


def test_writes_also_need_the_request_header(bare: TestClient, tmp_path: Path) -> None:
    auth = {"Authorization": f"Bearer {TEST_TOKEN}"}
    body = {"path": str(tmp_path / "x"), "create": True}
    refused = bare.post("/api/workspaces", json=body, headers=auth)
    assert refused.status_code == 403
    assert refused.json()["code"] == "missing_request_header"
    accepted = bare.post("/api/workspaces", json=body, headers={**auth, **WRITE_HEADER})
    assert accepted.status_code == 201, accepted.text


@pytest.mark.parametrize("host", ["evil.example", "192.168.1.5:8000", "127.0.0.1.evil.example"])
def test_non_loopback_hosts_are_refused_everywhere(bare: TestClient, host: str) -> None:
    for path in ("/health", "/api/workspaces"):
        response = bare.get(path, headers={"Host": host, "Authorization": f"Bearer {TEST_TOKEN}"})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_host"


@pytest.mark.parametrize("host", ["127.0.0.1:51234", "localhost:3000", "[::1]:8000", "LOCALHOST"])
def test_loopback_hosts_are_accepted(bare: TestClient, host: str) -> None:
    assert bare.get("/health", headers={"Host": host}).status_code == 200


# -- browser mode: launch URL → cookie --------------------------------------------


def test_the_launch_url_sets_a_strict_http_only_cookie(settings: Settings) -> None:
    app = create_app(replace(settings, token=None, port=51234))
    browser = TestClient(app, base_url=BASE)
    launch = app.state.access.launch
    response = browser.get(f"/api/session?token={launch}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"ik_session_51234={app.state.access.session};")
    for flag in ("HttpOnly", "SameSite=strict", "Path=/"):
        assert flag.lower() in cookie.lower()
    assert launch not in cookie
    # The browser now carries the cookie: reads work; writes still need the header.
    assert browser.get("/api/workspaces").status_code == 200
    assert browser.post("/api/workspaces", json={"path": "/nowhere"}).status_code == 403
    # Single use: the same launch link is refused afterwards.
    again = TestClient(app, base_url=BASE).get(
        f"/api/session?token={launch}", follow_redirects=False
    )
    assert again.status_code == 401


def test_a_bad_launch_token_sets_nothing(bare: TestClient) -> None:
    response = bare.get("/api/session?token=forged", follow_redirects=False)
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


# -- Content-Security-Policy -------------------------------------------------------


def test_inline_script_hashes_cover_every_inline_script(ui_dir: Path) -> None:
    hashes = inline_script_hashes(ui_dir)
    assert hashes == sorted(
        {
            _sha("self.__next_f=self.__next_f||[]"),
            _sha('self.__next_f.push([1,"x"])'),
            _sha("\nconsole.log('sub')\n"),
        }
    )


def test_build_csp_is_strict() -> None:
    csp = build_csp(["sha256-abc"])
    directives = dict(part.strip().split(" ", 1) for part in csp.split(";"))
    assert directives["script-src"] == "'self' 'sha256-abc'"
    assert directives["default-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["connect-src"] == "'self'"
    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-eval'" not in csp


def test_every_response_carries_the_csp(settings: Settings, ui_dir: Path) -> None:
    app = create_app(replace(settings, static_dir=ui_dir))
    client = TestClient(app, base_url=BASE)
    for response in (client.get("/"), client.get("/health"), client.get("/api/workspaces")):
        csp = response.headers["content-security-policy"]
        assert _sha('self.__next_f.push([1,"x"])') in csp
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get("/").status_code == 200  # the UI itself needs no token


def test_report_only_mode(settings: Settings) -> None:
    client = TestClient(create_app(replace(settings, csp_report_only=True)), base_url=BASE)
    response = client.get("/health")
    assert "content-security-policy" not in response.headers
    assert "default-src 'self'" in response.headers["content-security-policy-report-only"]


def test_blobs_are_sandboxed(workspace: Workspace) -> None:
    graph = workspace.client.get(f"/api/workspaces/{workspace.workspace_id}/graph").json()
    blob_hash = next(n["blob_hash"] for n in graph["nodes"] if n["blob_hash"])
    response = workspace.client.get(f"/api/workspaces/{workspace.workspace_id}/blobs/{blob_hash}")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "sandbox" in csp and "default-src 'none'" in csp and "script-src" not in csp


def test_real_static_export_scripts_are_all_hashed() -> None:
    """Against the real `next build` output, when present (Risk 1 safeguard)."""
    out = Path(__file__).resolve().parents[2] / "webUI" / "custom-graph-explorer" / "out"
    if not (out / "index.html").is_file():
        pytest.skip("no built UI (run `npm run build` in webUI/custom-graph-explorer)")
    hashes = inline_script_hashes(out)
    assert hashes, "a Next static export is expected to contain inline scripts"
    csp = build_csp(hashes)
    assert all(h in csp for h in hashes)


# -- launching -----------------------------------------------------------------------


def test_main_binds_a_random_port_prints_and_opens_the_launch_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("INDIEK_API_PORT", raising=False)
    monkeypatch.delenv("INDIEK_API_TOKEN", raising=False)
    monkeypatch.setenv("INDIEK_API_REGISTRY", str(tmp_path / "workspaces.json"))
    monkeypatch.setenv("INDIEK_API_ENV_FILE", str(tmp_path / "absent.env"))
    served: dict[str, object] = {}
    opened: list[str] = []

    def fake_serve(app: object, sockets: list, **_: bool) -> None:
        served["port"] = sockets[0].getsockname()[1]
        served["app"] = app

    monkeypatch.setattr(app_module, "_serve", fake_serve)
    monkeypatch.setattr(app_module.webbrowser, "open", opened.append)
    app_module.main([])
    port = served["port"]
    assert isinstance(port, int) and port > 0
    launch = served["app"].state.access.launch  # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{port}/api/session?token={launch}"
    assert url in capsys.readouterr().out
    assert opened == [url]


def test_main_no_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("INDIEK_API_PORT", raising=False)
    monkeypatch.setenv("INDIEK_API_REGISTRY", str(tmp_path / "workspaces.json"))
    monkeypatch.setenv("INDIEK_API_ENV_FILE", str(tmp_path / "absent.env"))
    opened: list[str] = []
    monkeypatch.setattr(app_module, "_serve", lambda app, sockets, **_: None)
    monkeypatch.setattr(app_module.webbrowser, "open", opened.append)
    app_module.main(["--no-browser"])
    assert opened == []
