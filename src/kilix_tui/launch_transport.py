"""Preserve POSIX argv bytes across the remote client's Unicode JSON wire."""
from __future__ import annotations

import json

_EXEC = ("import json,os,sys; a,e,c=json.loads(sys.argv[1]); "
         "os.environ.update(e); "
         "os.chdir(c) if c is not None else None; "
         "os.execvpe(a[0],a,os.environ)")


def _unicode(value: str) -> bool:
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def literal_launch(argv: list[str], environment: dict[str, str],
                   cwd: str | None = None) -> tuple[list[str], dict[str, str], str | None]:
    """Use an ASCII envelope only when the client would replace raw bytes.

    The owned child restores surrogate-escaped POSIX strings and execs the
    target directly. No shell parses either the payload or target arguments.
    ASCII environment markers stay visible to remote ownership matching.
    """
    if all(_unicode(value) for value in [*argv, *environment.values(), cwd or ""]):
        return argv, environment, cwd
    payload = json.dumps([argv, environment, cwd], ensure_ascii=True)
    safe_environment = {key: value if _unicode(value) else ""
                        for key, value in environment.items()}
    return ["python3", "-c", _EXEC, payload], safe_environment, None
