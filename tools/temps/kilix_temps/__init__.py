"""Kilix Temps: a Linux thermal dashboard for text and Kitty graphics."""

from pathlib import Path


def _repository_version() -> str:
    """Read the one version shared by every kilix-tui-utils command."""

    try:
        return (
            Path(__file__).resolve().parents[3] / "VERSION"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        return "0+unknown"


__version__ = _repository_version()
