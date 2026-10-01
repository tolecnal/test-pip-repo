"""devpi-smoke: a hello-world package for exercising an internal pip repository."""

from .core import __version__, build_info, hello

__all__ = ["__version__", "build_info", "hello"]
