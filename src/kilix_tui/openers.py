"""Shared, argv-only document opening for terminal applications."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence


def document_argv(path: str, *, environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Return the host/editor argv for a document without invoking a shell."""
    env = os.environ if environ is None else environ
    editor = env.get("VISUAL") or env.get("EDITOR")
    if editor:
        try:
            words = shlex.split(editor)
        except ValueError:
            words = []
        if words:
            return (*words, path)
    kilix = shutil.which("kilix", path=env.get("PATH"))
    if kilix:
        return (kilix, "run", path)
    for candidate in ("nano", "vi", "less"):
        if executable := shutil.which(candidate, path=env.get("PATH")):
            return (executable, path)
    return ("kilix", "run", path)


def open_document(
    path: str,
    *,
    argv: Sequence[str] | None = None,
) -> tuple[bool, str]:
    """Detach a document opener and return a short user-facing result."""
    command = tuple(argv) if argv is not None else document_argv(path)
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        return False, f"cannot open: {error}"
    return True, f"opened {os.path.basename(path) or path}"
