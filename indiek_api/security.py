"""Access control of the local API (DESKTOP:LOCALHOST-TOKEN).

Design and owner decisions 2026-10-25: project_management/Design.md, cybersecurity section.

* Only loopback ``Host`` headers are served (defeats DNS rebinding).
* Every ``/api/*`` route needs the per-launch token, either as ``Authorization: Bearer``
  (the future Tauri shell) or as the HttpOnly, SameSite=Strict session cookie set by the
  single-use launch URL ``/api/session?token=…`` (browser mode).
* Unsafe methods also need ``X-IndieK-Request: 1``: browsers make cross-origin pages ask
  first (CORS preflight), which this API never grants, so other pages cannot write.
* Every response carries a strict Content-Security-Policy whose ``script-src`` lists the
  SHA-256 of each inline script of the static UI (computed at startup); blobs are sandboxed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
from collections.abc import Iterable
from pathlib import Path

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_HEADER = "x-indiek-request"
SESSION_PATH = "/api/session"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MIN_FIXED_TOKEN_LENGTH = 16

_INLINE_SCRIPT = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)
_BLOB_PATH = re.compile(r"^/api/workspaces/[^/]+/blobs/")
# Archive content is untrusted: a blob opened on its own (e.g. an SVG in a new tab) runs nothing.
BLOB_CSP = "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def cookie_name(port: int | None) -> str:
    """One cookie per port: cookies are scoped by host, not by port."""
    return f"ik_session_{port}" if port else "ik_session"


class AccessTokens:
    """The session token (Bearer / cookie value) and the single-use launch token.

    ``fixed`` (``INDIEK_API_TOKEN``, development and tests only) serves as both and
    stays reusable, so a fixed launch URL can be reopened.
    """

    def __init__(self, fixed: str | None = None) -> None:
        self._fixed = fixed is not None
        self.session = fixed if fixed is not None else new_token()
        self.launch: str | None = fixed if fixed is not None else new_token()
        self._lock = threading.Lock()

    def check_session(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode(), self.session.encode())

    def redeem_launch(self, candidate: str | None) -> bool:
        with self._lock:
            if not candidate or self.launch is None:
                return False
            if not hmac.compare_digest(candidate.encode(), self.launch.encode()):
                return False
            if not self._fixed:
                self.launch = None
            return True


def inline_script_hashes(static_dir: Path | None) -> list[str]:
    """CSP sources (``'sha256-…'`` without quotes) of every inline script of the UI's HTML files."""
    if static_dir is None or not static_dir.is_dir():
        return []
    found: set[str] = set()
    for page in static_dir.rglob("*.html"):
        for body in _INLINE_SCRIPT.findall(page.read_text(encoding="utf-8")):
            digest = hashlib.sha256(body.encode("utf-8")).digest()
            found.add("sha256-" + base64.b64encode(digest).decode("ascii"))
    return sorted(found)


def build_csp(script_hashes: Iterable[str]) -> str:
    scripts = " ".join(["'self'", *(f"'{h}'" for h in script_hashes)])
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src {scripts}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


def _host_is_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:port
        host = host[: host.find("]") + 1]
    else:
        host = host.rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS


class AccessControlMiddleware:
    """Pure ASGI middleware enforcing the rules of the module docstring."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        tokens: AccessTokens,
        port: int | None,
        csp: str,
        csp_report_only: bool = False,
    ) -> None:
        self.app = app
        self.tokens = tokens
        self.cookie = cookie_name(port)
        self.csp = csp
        self.csp_header = (
            b"content-security-policy-report-only"
            if csp_report_only
            else b"content-security-policy"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        csp = BLOB_CSP if _BLOB_PATH.match(path) else self.csp

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (self.csp_header, b"x-content-type-options")
                ]
                headers.append((self.csp_header, csp.encode()))
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        connection = HTTPConnection(scope)
        headers = Headers(scope=scope)
        if not _host_is_loopback(headers.get("host")):
            await _json(send_with_headers, 400, "invalid_host", "Only loopback hosts are served.")
            return
        if path == SESSION_PATH and scope["method"] == "GET":
            await self._start_session(connection, send_with_headers)
            return
        if path == "/api" or path.startswith("/api/"):
            if not self._authenticated(connection, headers):
                await _json(
                    send_with_headers,
                    401,
                    "unauthorized",
                    "Missing or invalid API token: reopen the app from its launch link.",
                    extra_headers=[(b"www-authenticate", b"Bearer")],
                )
                return
            if scope["method"] in UNSAFE_METHODS and headers.get(REQUEST_HEADER) != "1":
                await _json(
                    send_with_headers,
                    403,
                    "missing_request_header",
                    f"Write requests need the header {REQUEST_HEADER}: 1.",
                )
                return
        await self.app(scope, receive, send_with_headers)

    def _authenticated(self, connection: HTTPConnection, headers: Headers) -> bool:
        authorization = headers.get("authorization", "")
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and self.tokens.check_session(credentials.strip()):
            return True
        return self.tokens.check_session(connection.cookies.get(self.cookie))

    async def _start_session(self, connection: HTTPConnection, send: Send) -> None:
        if not self.tokens.redeem_launch(connection.query_params.get("token")):
            await _json(
                send,
                401,
                "unauthorized",
                "This launch link is invalid or was already used: restart the app.",
                extra_headers=[
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                ],
            )
            return
        cookie = f"{self.cookie}={self.tokens.session}; HttpOnly; SameSite=Strict; Path=/"
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", b"/"),
                    (b"set-cookie", cookie.encode()),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


async def _json(
    send: Send,
    status: int,
    code: str,
    detail: str,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps({"detail": detail, "code": code}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *(extra_headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
