"""AnimeMachine application package."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("animemachine")
except PackageNotFoundError:  # Source tree before editable installation.
    __version__ = (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
