"""Failed tool calls in agent transcripts, grouped so the common ones stand out.

``scan(provider, path)`` streams one transcript and returns every tool call it
records, counted by tool, and one record per failed call. The first provider
reads a session JSONL (a subagent's file reads the same way), Codex a rollout,
and Grok a session directory's ``updates.jsonl``. Each provider's own outcome
fields decide failure: an error flag, an exit status, a failed item status or a
named failure variant. Output text is never scored for words like "error".

``group(failures)`` folds the records into signatures: tool, failure kind and
the most telling output line with paths, numbers, IDs and quoted values
replaced, so a hundred runs of one bug read as one row with a count.

Everything is bounded: lines are streamed, kept text is truncated, and only a
few examples are kept per signature. Text is copied from the transcript, so it
is data to read, not instructions.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime

from . import jsonl

PROVIDERS = ("claude", "codex", "grok")
MESSAGE_LIMIT = 600
COMMAND_LIMIT = 300
EXAMPLES = 3

# Lines that usually say what went wrong, preferred over the first line.
_TELLING = re.compile(
    r"error|fatal|fail|traceback|exception|denied|not found|no such|cannot|can't|"
    r"unable|refus|invalid|timed? ?out|unsupported|unknown|missing|conflict|abort",
    re.IGNORECASE)
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt)\b(:.*)?$")
_PROGRAM_ERROR = re.compile(
    r"^(?:fatal|error|Error|ERROR|FAIL|FAILED|E|panic)\b[:!]?.*|"
    r"^[\w./+-]{1,40}: (?:.*: )?(?:[Ee]rror|[Cc]annot|can't|No such|[Nn]ot found|[Pp]ermission denied|"
    r"[Ii]nvalid|[Uu]nrecognized|[Uu]nknown|[Mm]issing|refusing|failed|unable|is not|not a)\b.*")
_NORMALIZE = (
    (re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"), "<id>"),
    (re.compile(r"(?<![\w/])(?:~|\.{1,2})?/[^\s'\"`:,;()\[\]{}<>]+"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b"), "<hex>"),
    (re.compile(r"'[^'\n]{1,200}'|\"[^\"\n]{1,200}\"|`[^`\n]{1,200}`"), "<q>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def _clip(text: object, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _texts(value: object) -> str:
    """Join text from a string or a list of text parts (any provider's shape)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                inner = item.get("text")
                if not isinstance(inner, str) and isinstance(item.get("content"), (dict, list, str)):
                    inner = _texts(item["content"] if isinstance(item["content"], list)
                                   else [item["content"]])
                if isinstance(inner, str):
                    parts.append(inner)
        return "\n".join(parts)
    if isinstance(value, dict):
        return _texts([value])
    return ""


def _structured_error(text: str) -> str:
    """The code and message of a JSON error envelope, if the text is one."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return ""
    try:
        value = json.loads(stripped)
    except ValueError:
        return ""
    if not isinstance(value, dict):
        return ""
    for key in ("Error", "error"):
        if isinstance(value.get(key), str):
            return _structured_error(value[key]) or value[key]
    errors = value.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        value = errors[0]
    code, message = value.get("code"), value.get("message")
    if isinstance(code, str) or isinstance(message, str):
        return ": ".join(part for part in (code, message) if isinstance(part, str))
    return ""


def telling_line(text: str) -> str:
    """The line most likely to name the failure.

    A JSON error envelope gives its code and message. Otherwise the last
    exception line, else the last line a program prints as an error
    ("prog: ...", "fatal: ...", "error: ..."), else the last error-sounding
    line, else the last line. Failures print at the end, and earlier lines are
    often a file listing that merely contains words like "error"."""
    structured = _structured_error(text)
    if structured:
        return structured
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    for test in (_EXCEPTION_LINE.match, _PROGRAM_ERROR.match, _TELLING.search):
        for line in reversed(lines):
            if test(line):
                return line
    return lines[-1]


def normalize(text: str) -> str:
    for pattern, replacement in _NORMALIZE:
        text = pattern.sub(replacement, text)
    return text.strip()[:160]


def _epoch(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000.0 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _command(name: str, arguments: object) -> str:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return _clip(arguments, COMMAND_LIMIT)
    if isinstance(arguments, dict):
        for key in ("command", "cmd", "file_path", "target_file", "path", "pattern", "url",
                    "query", "session", "request"):
            value = arguments.get(key)
            if isinstance(value, list):
                value = " ".join(str(part) for part in value)
            if isinstance(value, str) and value:
                return _clip(value, COMMAND_LIMIT)
        return _clip(json.dumps(arguments, ensure_ascii=False, sort_keys=True), COMMAND_LIMIT)
    return ""


class _Scan:
    def __init__(self, provider: str, path: str, since: float):
        self.provider, self.path, self.since = provider, path, since
        self.calls: Counter[str] = Counter()
        self.failures: list[dict] = []
        self.session = ""

    def in_window(self, stamp: object) -> bool:
        when = _epoch(stamp)
        return not self.since or when is None or when >= self.since

    def call(self, tool: str, stamp: object) -> None:
        if self.in_window(stamp):
            self.calls[tool or "?"] += 1

    def fail(self, line: int, stamp: object, tool: str, kind: str, text: str, *,
             command: str = "", exit_code: int | None = None) -> None:
        if not self.in_window(stamp):
            return
        text = text if isinstance(text, str) else ""
        if kind == "exit_code" and exit_code == 1 and not _meaningful(text):
            # grep, rg, diff, cmp and test report "no match"/"differs" this way.
            kind = "silent_exit_1"
        self.failures.append({
            "provider": self.provider, "path": self.path, "line": line,
            "session": self.session, "timestamp": stamp if isinstance(stamp, str) else
            (datetime.fromtimestamp(_epoch(stamp)).isoformat(timespec="seconds")
             if _epoch(stamp) is not None else None),
            "tool": tool or "?", "kind": kind, "exit_code": exit_code,
            "command": command, "message": _clip(text, MESSAGE_LIMIT),
            "telling": _clip(telling_line(text), 300),
        })


def _meaningful(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped not in ("(no output)", "(no content)")


BENIGN_KINDS = frozenset({"silent_exit_1"})


def _first_kind(text: str) -> tuple[str, int | None]:
    """Classify a failed first-provider tool result from its fixed prefixes."""
    stripped = text.lstrip()
    exit_match = re.match(r"Exit code (\d+)", stripped)
    if exit_match:
        return "exit_code", int(exit_match.group(1))
    if stripped.startswith("<tool_use_error>"):
        return "tool_use_error", None
    if stripped.startswith(("The user doesn't want to proceed", "User rejected",
                            "[Request interrupted by user")):
        return "user_rejected", None
    if "hook" in stripped[:200].lower() and "block" in stripped[:400].lower():
        return "hook_blocked", None
    return "error", None


def _scan_first(scan: _Scan, handle) -> None:
    pending: dict[str, tuple[str, str]] = {}
    for number, raw in enumerate(handle, 1):
        row = jsonl.load(raw)
        if row is None:
            continue
        if not scan.session and isinstance(row.get("sessionId"), str):
            scan.session = row["sessionId"]
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        stamp = row.get("timestamp")
        for block in content:
            if not isinstance(block, dict):
                continue
            if row.get("type") == "assistant" and block.get("type") == "tool_use":
                name = block.get("name") if isinstance(block.get("name"), str) else "?"
                if isinstance(block.get("id"), str):
                    pending[block["id"]] = (name, _command(name, block.get("input")))
                scan.call(name, stamp)
            elif (row.get("type") == "user" and block.get("type") == "tool_result"
                    and block.get("is_error") is True):
                name, command = pending.get(block.get("tool_use_id"), ("?", ""))
                text = _texts(block.get("content"))
                kind, code = _first_kind(text)
                if kind == "exit_code":
                    text = text.lstrip().partition("\n")[2]
                if kind == "error" and name.startswith("mcp__"):
                    kind = "mcp_error"
                scan.fail(number, stamp, name, kind, text, command=command, exit_code=code)


def _scan_codex(scan: _Scan, handle) -> None:
    calls: dict[str, tuple[str, str]] = {}
    fallback: list[tuple] = []
    saw_items = False
    for number, raw in enumerate(handle, 1):
        row = jsonl.load(raw)
        if row is None:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        stamp, kind = row.get("timestamp"), row.get("type")
        if kind == "session_meta" and isinstance(payload.get("id"), str):
            scan.session = payload["id"]
        elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            name = payload.get("name") if isinstance(payload.get("name"), str) else "?"
            arguments = payload.get("arguments", payload.get("input"))
            if isinstance(payload.get("call_id"), str):
                calls[payload["call_id"]] = (name, _command(name, arguments))
            if name not in ("exec_command", "shell", "local_shell"):
                scan.call(name, stamp)
        elif kind == "response_item" and payload.get("type") in (
                "function_call_output", "custom_tool_call_output"):
            name, command = calls.get(payload.get("call_id"), ("?", ""))
            text = _texts(payload.get("output"))
            head = text.lstrip()
            if head.startswith("Script failed"):
                scan.fail(number, stamp, name, "script_failed", head[len("Script failed"):],
                          command=command)
            elif head.startswith("aborted by user"):
                scan.fail(number, stamp, name, "user_rejected", text, command=command)
            else:
                # Rollouts without the item stream report shell status in text.
                code = re.search(r"(?m)^Process exited with code (\d+)", text)
                if code and int(code.group(1)) != 0:
                    fallback.append((number, stamp, name, text, command, int(code.group(1))))
        elif kind == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            type_, status = item.get("type"), item.get("status")
            if type_ == "CommandExecution":
                saw_items = True
                scan.call("shell", stamp)
                code = item.get("exit_code") if type(item.get("exit_code")) is int else None
                if status == "failed" or (code not in (None, 0)):
                    command = item.get("command")
                    if (isinstance(command, list) and len(command) == 3
                            and command[1] in ("-c", "-lc") and isinstance(command[2], str)):
                        command = command[2]
                    command = _command("shell", {"command": command})
                    scan.fail(number, stamp, "shell", "exit_code", item.get("aggregated_output") or "",
                              command=_clip(command, COMMAND_LIMIT), exit_code=code)
            elif type_ == "McpToolCall":
                tool = f"mcp:{item.get('server')}/{item.get('tool')}"
                scan.call(tool, stamp)
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                if status == "failed" or result.get("isError") is True or item.get("error"):
                    text = _texts(result.get("content")) or _texts(item.get("error")) or \
                        json.dumps(item.get("error"), ensure_ascii=False)[:MESSAGE_LIMIT]
                    scan.fail(number, stamp, tool, "mcp_error", text,
                              command=_command(tool, item.get("arguments")))
            elif type_ == "FileChange":
                scan.call("apply_patch", stamp)
                if status == "failed":
                    scan.fail(number, stamp, "apply_patch", "patch_failed",
                              _texts(item.get("output")) or json.dumps(item.get("changes"))[:MESSAGE_LIMIT])
            elif type_ == "Extension":
                tool = f"extension:{item.get('kind')}"
                scan.call(tool, stamp)
                if item.get("failure"):
                    scan.fail(number, stamp, tool, "extension_failed", _texts(item.get("failure"))
                              or json.dumps(item.get("failure"), ensure_ascii=False)[:MESSAGE_LIMIT])
    if not saw_items:
        for number, stamp, name, text, command, code in fallback:
            scan.call(name, stamp)
            scan.fail(number, stamp, name, "exit_code", text, command=command, exit_code=code)


def _scan_grok(scan: _Scan, handle) -> None:
    names: dict[str, tuple[str, str]] = {}
    for number, raw in enumerate(handle, 1):
        row = jsonl.load(raw)
        if row is None:
            continue
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        if not scan.session and isinstance(params.get("sessionId"), str):
            scan.session = params["sessionId"]
        stamp, event = row.get("timestamp"), update.get("sessionUpdate")
        call_id = update.get("toolCallId")
        if event == "tool_call" and isinstance(call_id, str) and call_id not in names:
            meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
            tool = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), dict) else {}
            name = tool.get("name") or update.get("title") or "?"
            raw = update.get("rawInput")
            if isinstance(raw, dict) and isinstance(raw.get("tool_input"), dict):
                raw = raw["tool_input"]     # an MCP call's own arguments
            names[call_id] = (str(name), _command(str(name), raw))
            scan.call(str(name), stamp)
        elif event == "tool_call_update" and update.get("status") in ("completed", "failed"):
            name, command = names.get(call_id, ("?", ""))
            output = update.get("rawOutput") if isinstance(update.get("rawOutput"), dict) else {}
            type_ = output.get("type")
            text = _texts(update.get("content"))
            if type_ == "MCP":
                mcp = f"mcp:{output.get('server_name')}/{output.get('tool_name')}"
                if name != mcp and call_id in names:
                    # Count the call under its server and tool, once.
                    if scan.calls.get(name, 0) > 0 and scan.in_window(stamp):
                        scan.calls[name] -= 1
                        if not scan.calls[name]:
                            del scan.calls[name]
                    scan.call(mcp, stamp)
                    names[call_id] = (mcp, command)
                name = mcp
                if update["status"] == "failed" or output.get("is_error") in (True, "True", "true"):
                    detail = output.get("output")
                    if isinstance(detail, dict):
                        detail = "\n".join(str(value) for value in detail.values())
                    scan.fail(number, stamp, name, "mcp_error",
                              detail if isinstance(detail, str) else _texts(detail) or text,
                              command=command)
            elif update["status"] == "failed":
                variants = [key for key in output if key not in ("type", "exit_code", "file_matches",
                                                                 "match_count", "stderr")]
                variant = variants[0] if variants else "failed"
                detail = output.get(variant)
                text = (detail if isinstance(detail, str) else _texts(detail)) or text
                scan.fail(number, stamp, name, f"failed:{type_ or '?'}/{variant}", text, command=command,
                          exit_code=output.get("exit_code") if type(output.get("exit_code")) is int else None)
            elif type_ == "Bash" and type(output.get("exit_code")) is int and output["exit_code"] != 0:
                scan.fail(number, stamp, name, "exit_code", text,
                          command=_clip(output.get("command") or command, COMMAND_LIMIT),
                          exit_code=output["exit_code"])
            elif (type_ == "GrepSearch" and type(output.get("exit_code")) is int
                    and output["exit_code"] > 1):
                # Exit 1 is "no matches"; only a broken search is a failure.
                scan.fail(number, stamp, name, "exit_code", _texts(output.get("stderr")) or text,
                          command=command, exit_code=output["exit_code"])


def scan(provider: str, path: str, *, since: float = 0.0) -> dict:
    """Return {"calls": Counter, "failures": [...]} for one transcript.

    ``since`` is an epoch cutoff applied to each row's own timestamp; rows
    without one are kept. A Grok path may be the session directory.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")
    if provider == "grok" and os.path.isdir(path):
        path = os.path.join(path, "updates.jsonl")
    state = _Scan(provider, path, since)
    with open(path, "rb") as handle:
        {"claude": _scan_first, "codex": _scan_codex, "grok": _scan_grok}[provider](state, handle)
    if provider == "grok" and not state.session:
        state.session = os.path.basename(os.path.dirname(path))
    for failure in state.failures:
        failure["session"] = failure["session"] or state.session
    return {"calls": state.calls, "failures": state.failures}


# A Kilix program in command position: at the start, after a separator or
# "$(", optionally behind wrappers such as timeout, env or sudo.
_KILIX_COMMAND = re.compile(
    r"(?:^|[;&|(\n]|\$\()\s*"
    r"(?:(?:timeout\s+\S+|env(?:\s+-u\s+\S+|\s+\w+=\S*)*|sudo|exec|nohup|command|time|xargs)\s+)*"
    r"(?:\S*/)?(?:kilix|kitten|plebian-os)(?:-[\w-]+)?(?=\s|$|[;&|)])")


def is_kilix(failure: dict) -> bool:
    """Whether a failure came from a Kilix tool: a Kilix MCP server's tool or a
    shell command that runs a Kilix command (kilix, kilix-*, kitten, plebian-os)."""
    tool = failure.get("tool") or ""
    if "kilix" in tool.lower():
        return True
    return bool(_KILIX_COMMAND.search(failure.get("command") or ""))


def signature(failure: dict) -> str:
    return normalize(failure.get("telling") or failure.get("message") or "") or "(no output)"


def group(failures: list[dict], *, examples: int = EXAMPLES) -> list[dict]:
    """Fold failures into signature rows, most frequent first."""
    rows: dict[tuple, dict] = {}
    for failure in failures:
        key = (failure["provider"], failure["tool"], failure["kind"], signature(failure))
        row = rows.get(key)
        if row is None:
            row = rows[key] = {"provider": key[0], "tool": key[1], "kind": key[2],
                               "signature": key[3], "count": 0, "sessions": set(),
                               "first": None, "last": None, "examples": []}
        row["count"] += 1
        row["sessions"].add(failure.get("session") or failure["path"])
        stamp = failure.get("timestamp")
        if stamp:
            row["first"] = min(filter(None, (row["first"], stamp)))
            row["last"] = max(filter(None, (row["last"], stamp)))
        if len(row["examples"]) < examples:
            row["examples"].append({key2: failure[key2] for key2 in (
                "path", "line", "timestamp", "command", "telling", "exit_code")})
    result = sorted(rows.values(), key=lambda row: (-row["count"], -len(row["sessions"]),
                                                     row["provider"], row["tool"], row["signature"]))
    for row in result:
        row["sessions"] = len(row["sessions"])
    return result


def discover(provider: str, *, since: float = 0.0, home: str = "") -> list[str]:
    """Transcript paths modified at or after ``since`` (epoch), oldest first.

    The first provider includes subagent transcripts (``<session>/subagents``).
    Codex includes archived rollouts. Grok yields session directories.
    """
    found: list[tuple[float, str]] = []

    def keep(path: str) -> None:
        try:
            when = os.stat(path).st_mtime
        except OSError:
            return
        if not since or when >= since:
            found.append((when, path))

    if provider == "claude":
        root = os.path.join(home or os.environ.get("CLAUDE_CONFIG_DIR")
                            or os.path.expanduser("~/.claude"), "projects")
        for folder in _listdir(root):
            project = os.path.join(root, folder)
            for name in _listdir(project):
                path = os.path.join(project, name)
                if name.endswith(".jsonl"):
                    keep(path)
                elif os.path.isdir(os.path.join(path, "subagents")):
                    for agent in _listdir(os.path.join(path, "subagents")):
                        if agent.endswith(".jsonl"):
                            keep(os.path.join(path, "subagents", agent))
    elif provider == "codex":
        root = home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        for top in ("sessions", "archived_sessions"):
            for dirpath, _dirs, files in os.walk(os.path.join(root, top)):
                for name in files:
                    if name.startswith("rollout-") and name.endswith(".jsonl"):
                        keep(os.path.join(dirpath, name))
    elif provider == "grok":
        root = os.path.join(home or os.environ.get("GROK_HOME") or os.path.expanduser("~/.grok"),
                            "sessions")
        for folder in _listdir(root):
            for session in _listdir(os.path.join(root, folder)):
                updates = os.path.join(root, folder, session, "updates.jsonl")
                if os.path.isfile(updates):
                    keep(updates)
        found = [(when, os.path.dirname(path)) for when, path in found]
    else:
        raise ValueError(f"unsupported provider: {provider}")
    return [path for _when, path in sorted(found)]


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []
