"""The signing identity of the desktop app (DESKTOP:KEY-ONBOARDING).

`KeyProvider` is the API's signing seam (DESKTOP:REUSE-SEAMS): routes ask it for the author
that signs writes and never read keys themselves. Owner decisions 2026-10-25: the key lives
in the OS keychain through `indiek.keys` (shared with the `ik` CLI); `IK_AUTHOR_PRIVATE_KEY`
wins; backups are password-encrypted files and the plaintext seed never leaves this process.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

from indiek import Author, KeychainError, KeychainUnavailableError, keys

from indiek_api.errors import (
    InvalidBackupError,
    KeychainUnavailableAPIError,
    KeyExistsError,
    MissingAuthorKeyError,
    WeakPasswordError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _KeychainState:
    author: Author | None
    available: bool


class KeyProvider:
    """Resolves the signing author: `IK_AUTHOR_PRIVATE_KEY` (re-read on every call, cheap),
    else the OS keychain (read once and cached, so the OS is not asked on every write;
    refreshed after every identity change made through this provider)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keychain: _KeychainState | None = None

    # -- reading --------------------------------------------------------------------

    def _env_author(self) -> Author | None:
        seed = os.getenv(keys.ENV_VAR, "").strip()
        if not seed:
            return None
        try:
            return Author.from_seed_hex(seed)
        except ValueError as exc:
            raise MissingAuthorKeyError(
                f"{keys.ENV_VAR} must be a 32-byte Ed25519 seed written as 64 hex characters."
            ) from exc

    def _keychain_state(self) -> _KeychainState:
        with self._lock:
            if self._keychain is None:
                try:
                    self._keychain = _KeychainState(keys.load_from_keychain(), True)
                except KeychainUnavailableError:
                    self._keychain = _KeychainState(None, False)
                except KeychainError as exc:  # a corrupted entry: report "no key", keep serving
                    logger.warning("Ignoring the keychain entry: %s", exc)
                    self._keychain = _KeychainState(None, True)
            return self._keychain

    def refresh(self) -> None:
        with self._lock:
            self._keychain = None

    def status(self) -> tuple[Author | None, keys.KeySource, bool]:
        """(author, source, keychain_available)."""
        state = self._keychain_state()
        env = self._env_author()
        if env is not None:
            return env, keys.KeySource.ENV, state.available
        if state.author is not None:
            return state.author, keys.KeySource.KEYCHAIN, state.available
        return None, keys.KeySource.NONE, state.available

    def author_or_none(self) -> Author | None:
        return self.status()[0]

    def author(self) -> Author:
        author = self.author_or_none()
        if author is None:
            raise MissingAuthorKeyError(
                "Archive writes need an author key: create or import one in the app, "
                f"or set {keys.ENV_VAR}."
            )
        return author

    # -- changing -------------------------------------------------------------------

    def store(self, author: Author, *, replace: bool) -> None:
        """Put `author` in the OS keychain (shared with `ik`)."""
        try:
            keys.store_in_keychain(author, overwrite=replace)
        except KeychainUnavailableError as exc:
            raise KeychainUnavailableAPIError(str(exc)) from exc
        except KeychainError as exc:
            raise KeyExistsError(
                "The OS keychain already holds an IndieK author key; back it up, then "
                "replace it explicitly."
            ) from exc
        finally:
            self.refresh()

    def backup(self, password: str) -> tuple[Author, bytes]:
        author = self.author()
        try:
            return author, keys.export_backup(author, password)
        except ValueError as exc:
            raise WeakPasswordError(str(exc)) from exc

    @staticmethod
    def read_backup(data: str, password: str) -> Author:
        try:
            return keys.import_backup(data.encode("utf-8"), password)
        except ValueError as exc:
            raise InvalidBackupError(str(exc)) from exc
