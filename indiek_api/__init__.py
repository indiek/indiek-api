"""Local HTTP API serving IndieK archives to the IndieK Desktop App."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version in pyproject.toml, read from the installed package.
    __version__ = version("indiek-api")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
