"""Runtime configuration for the IndieK local API."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from indiek.signing import get_signing_key

from indiek_api.errors import MissingAuthorKeyError

PROJECT_DIR = Path(__file__).resolve().parent.parent
# The API signs archive writes with the user's key: never expose it beyond this machine.
LOOPBACK = "127.0.0.1"


@dataclass(frozen=True)
class AuthorCredentials:
    public_key: str
    signing_key: Any


@dataclass(frozen=True)
class Settings:
    env_file: Path | None = PROJECT_DIR / ".env"
    registry_path: Path = Path.home() / ".indiek" / "workspaces.json"
    port: int = 8000
    static_dir: Path | None = None
    cors_origins: tuple[str, ...] = ()
    host: str = field(default=LOOPBACK, init=False)

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``INDIEK_API_*`` environment variables, falling back to defaults."""
        defaults = cls()

        def path_or(name: str, default: Path | None) -> Path | None:
            value = os.getenv(name, "").strip()
            return Path(value).expanduser() if value else default

        origins = os.getenv("INDIEK_API_CORS_ORIGINS", "")
        return cls(
            env_file=path_or("INDIEK_API_ENV_FILE", defaults.env_file),
            registry_path=path_or("INDIEK_API_REGISTRY", defaults.registry_path)
            or defaults.registry_path,
            port=int(os.getenv("INDIEK_API_PORT", defaults.port)),
            static_dir=path_or("INDIEK_API_STATIC_DIR", defaults.static_dir),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        )

    def load_env_file(self) -> None:
        """Load the dotenv file once; real environment variables take precedence."""
        if self.env_file is not None and self.env_file.is_file():
            load_dotenv(self.env_file, override=False)

    def author_credentials(self) -> AuthorCredentials:
        """Return the key that signs writes, read fresh from ``IK_AUTHOR_PRIVATE_KEY``."""
        seed = os.getenv("IK_AUTHOR_PRIVATE_KEY", "").strip()
        if not seed:
            raise MissingAuthorKeyError(
                "Archive writes need IK_AUTHOR_PRIVATE_KEY (64 hex characters) in the API environment."
            )
        try:
            signing_key = get_signing_key(seed)
        except ValueError as exc:
            raise MissingAuthorKeyError(
                "IK_AUTHOR_PRIVATE_KEY must be a 32-byte Ed25519 seed written as 64 hex characters."
            ) from exc
        return AuthorCredentials(signing_key.verify_key.encode().hex(), signing_key)
