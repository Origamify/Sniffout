"""SniffOut legacy shim — re-exports from the split modules."""

from .webui import app, main  # noqa: F401

__all__ = ["app", "main"]
