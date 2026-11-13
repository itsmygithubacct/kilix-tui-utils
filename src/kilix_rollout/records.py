"""Provider-neutral, explicit-row transcript export for cited log readers.

``adapt_record(provider, row)`` accepts one already-decoded JSON object and returns
``{"records": [...], "excluded": [...], "errors": [...]}``. Each exported item
has role, channel, text, timestamp, timestamp_basis, turn_id, tool_call_id,
and json_pointer. It performs no discovery, file IO, or text condensation.
Callers attach byte spans, source identities, and record IDs.
"""
from __future__ import annotations

from datetime import datetime, timezone


# Transcript rows (first provider below) that carry session bookkeeping, not
# conversation text.
_TRANSCRIPT_METADATA = frozenset({
    "system", "summary", "file-history-snapshot", "file-history-delta", "last-prompt",
    "attachment", "queue-operation", "ai-title", "custom-title", "agent-name", "mode",
    "permission-mode", "session-owner", "atis-latch", "tools-used", "cost-state",
    "continued-in", "frame-link", "artifact-comment-monitor", "artifact-autoreact-ledger"})
_API_REQUEST_COPIES = frozenset({"api-request", "api-request-blob", "api-request-shape"})
# Codex rollout rows and items that carry settings, usage, tool schemas or
# context state. Conversation text stays in response_item messages and calls.
_CODEX_TOP_METADATA = frozenset({
    "session_meta", "turn_context", "token_usage_record", "world_state",
    "inter_agent_communication_metadata"})
_CODEX_EVENT_METADATA = frozenset({
    "token_count", "context_compacted", "turn_aborted", "thread_settings_applied",
    "thread_goal_updated"})
# Grok session updates (updates.jsonl): ACP rows under session/update and
# Grok's own under _x.ai/session/update. Each message is one row.
_GROK_METHODS = frozenset({"session/update", "_x.ai/session/update"})
_GROK_METADATA = frozenset({
    "plan", "current_mode_update", "background_tasks", "task_backgrounded", "task_completed",
    "goal_updated", "auto_compact_started", "auto_compact_completed", "auto_compact_cancelled",
    "auto_compact_failed", "compaction_checkpoint", "retry_state", "subagent_spawned",
    "subagent_finished", "memory_flush_started", "memory_flush_completed", "memory_dream_queued",
    "memory_dream_started", "memory_dream_completed", "memory_session_saved",
    "scheduled_task_created", "scheduled_task_deleted"})
_GROK_SUMMARIES = frozenset({"agent_thought_chunk", "session_recap"})
_SYNTHETIC_USER = ("<system-reminder>", "<task-notification>", "<local-command-stdout>",
                   "<monitor-event")


def _pointer(*parts: object) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _row_time(row: dict) -> str | None:
    stamp = row.get("timestamp")
    if isinstance(stamp, str):
        return stamp
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 < stamp < 1e11:
        # Grok writes epoch seconds.
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def adapt_record(provider: str, row: dict) -> dict:
    """Export eligible text from a Claude, Codex, Grok or OMP JSONL row.

    Unsupported variants are explicit errors. Exclusions are categorical and
    should be copied into source coverage. Text is kept byte-for-byte after
    JSON decoding; embedded terminal controls remain for the renderer to escape.
    """
    result = {"records": [], "excluded": [], "errors": []}
    stamp = _row_time(row) if isinstance(row, dict) else None
    def emit(role: str, channel: str, text: object, pointer: str,
             *, tool_id: object = None, turn_id: object = None) -> None:
        if not isinstance(text, str):
            result["errors"].append({"code": "unsupported_record", "message": f"non-text content at {pointer}"})
            return
        result["records"].append({
            "role": role, "channel": channel, "text": text,
            "timestamp": stamp,
            "timestamp_basis": "source" if stamp else "unknown",
            "turn_id": turn_id if isinstance(turn_id, str) else None,
            "tool_call_id": tool_id if isinstance(tool_id, str) else None,
            "json_pointer": pointer,
        })
    def exclude(category: str) -> None:
        if category not in result["excluded"]:
            result["excluded"].append(category)
    def unsupported(what: object) -> None:
        result["errors"].append({"code": "unsupported_record", "message": f"unsupported {provider} record: {str(what)[:160]}"})
    if not isinstance(row, dict):
        unsupported("non-object")
        return result
    if provider == "grok":
        # Grok rows are JSON-RPC notifications with no "type" field.
        _adapt_grok(row, emit, exclude, unsupported)
        return result
    if provider in {"omp", "qwen"}:
        _adapt_omp(row, emit, exclude, unsupported)
        return result
    kind = row.get("type")
    if not isinstance(kind, str):
        unsupported("type")
        return result
    if provider == "claude":
        if kind in _TRANSCRIPT_METADATA:
            exclude("system_or_metadata")
        elif kind in _API_REQUEST_COPIES:
            # Copies of request messages, system prompts and tool schemas that
            # the user/assistant rows already record. Never a second citation.
            exclude("api_request_copy")
        elif kind in {"user", "assistant"}:
            if row.get("isMeta") or row.get("isSidechain"):
                exclude("synthetic_or_sidechain")
                return result
            msg = row.get("message")
            if not isinstance(msg, dict) or (msg.get("role") is not None and
                    (not isinstance(msg.get("role"), str) or msg.get("role") != kind)):
                unsupported("message role mismatch")
                return result
            content = msg.get("content")
            if isinstance(content, str):
                if kind == "user" and content.lstrip().startswith(("<system-reminder>", "<task-notification>", "<local-command-stdout>")):
                    exclude("synthetic_user_wrapper")
                else:
                    emit(kind, "message", content, _pointer("message", "content"))
            elif isinstance(content, list):
                for i, block in enumerate(content):
                    if not isinstance(block, dict):
                        unsupported("content block")
                        continue
                    block_kind = block.get("type")
                    base = _pointer("message", "content", i)
                    if not isinstance(block_kind, str):
                        unsupported("content block type")
                        continue
                    if block_kind == "text":
                        value = block.get("text")
                        if kind == "user" and isinstance(value, str) and value.lstrip().startswith(("<system-reminder>", "<task-notification>", "<local-command-stdout>")):
                            exclude("synthetic_user_wrapper")
                        else:
                            emit(kind, "message", value, base + "/text")
                    elif block_kind == "tool_use" and kind == "assistant":
                        # The name is an exact source string; input is data, not an assistant claim.
                        emit("assistant", "tool_request", block.get("name"), base + "/name", tool_id=block.get("id"))
                        parameters = block.get("input")
                        if isinstance(parameters, dict) and isinstance(parameters.get("command"), str):
                            emit("assistant", "tool_request", parameters["command"], base + "/input/command", tool_id=block.get("id"))
                    elif block_kind == "tool_result" and kind == "user":
                        value = block.get("content")
                        if isinstance(value, str):
                            emit("tool", "tool_result", value, base + "/content", tool_id=block.get("tool_use_id"))
                        elif isinstance(value, list):
                            for j, item in enumerate(value):
                                if isinstance(item, dict) and isinstance(item.get("type"), str) and item.get("type") == "text":
                                    emit("tool", "tool_result", item.get("text"), base + _pointer("content", j, "text"), tool_id=block.get("tool_use_id"))
                                else:
                                    exclude("nontext_tool_payload")
                        else:
                            exclude("nontext_tool_payload")
                    elif block_kind in {"thinking", "redacted_thinking", "image", "document"}:
                        exclude("reasoning_or_binary")
                    else:
                        unsupported(block_kind)
            else:
                unsupported("message content")
        else:
            unsupported(kind)
    elif provider == "codex":
        payload = row.get("payload")
        if not isinstance(payload, dict):
            unsupported("payload")
            return result
        if kind == "response_item":
            item_type = payload.get("type")
            if not isinstance(item_type, str):
                unsupported("payload type")
                return result
            channel = payload.get("channel")
            if not (channel is None or isinstance(channel, str)):
                unsupported("response channel")
                return result
            if channel in {"analysis", "reasoning"}:
                exclude("reasoning")
                return result
            if channel not in {None, "final", "commentary"}:
                unsupported("response channel")
                return result
            if item_type == "message":
                role = payload.get("role")
                if not isinstance(role, str):
                    unsupported("message role")
                    return result
                if role in {"system", "developer"}:
                    exclude("system_or_developer")
                    return result
                if role not in {"user", "assistant"}:
                    unsupported("message role")
                    return result
                content = payload.get("content")
                if not isinstance(content, list):
                    unsupported("message content")
                    return result
                for i, part in enumerate(content):
                    if not isinstance(part, dict):
                        unsupported("content part")
                        continue
                    typ = part.get("type")
                    if not isinstance(typ, str):
                        unsupported("content part type")
                        continue
                    if typ in {"input_text", "output_text"} and ((role == "user" and typ == "input_text") or (role == "assistant" and typ == "output_text")):
                        value = part.get("text")
                        if role == "user" and isinstance(value, str) and value.lstrip().startswith(("<environment_context>", "<permissions instructions>", "<skills_instructions>", "<codex_internal_context")):
                            exclude("synthetic_user_context")
                        else:
                            emit(role, "message", value, _pointer("payload", "content", i, "text"))
                    elif typ in {"input_image", "output_image", "image", "audio", "input_audio"}:
                        exclude("binary_payload")
                    else:
                        unsupported(typ)
            elif item_type in {"function_call", "custom_tool_call"}:
                emit("assistant", "tool_request", payload.get("name"), _pointer("payload", "name"), tool_id=payload.get("call_id"))
                if isinstance(payload.get("arguments"), str):
                    emit("assistant", "tool_request", payload["arguments"], _pointer("payload", "arguments"), tool_id=payload.get("call_id"))
                if isinstance(payload.get("input"), str):
                    emit("assistant", "tool_request", payload["input"], _pointer("payload", "input"), tool_id=payload.get("call_id"))
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                output = payload.get("output")
                if isinstance(output, list):
                    # Current rollouts store tool output as content parts.
                    for i, part in enumerate(output):
                        typ = part.get("type") if isinstance(part, dict) else None
                        if not isinstance(typ, str):
                            unsupported("output part")
                        elif typ in {"input_text", "output_text"}:
                            emit("tool", "tool_result", part.get("text"), _pointer("payload", "output", i, "text"),
                                 tool_id=payload.get("call_id"))
                        elif typ in {"input_image", "output_image", "image", "audio", "input_audio"}:
                            exclude("binary_payload")
                        else:
                            unsupported(typ)
                else:
                    emit("tool", "tool_result", output, _pointer("payload", "output"), tool_id=payload.get("call_id"))
            elif item_type == "agent_message":
                # A message between agents of one Codex run (author/recipient
                # paths). It is neither this thread's user nor its assistant,
                # so it stays searchable without being cited as either.
                content = payload.get("content")
                if not isinstance(content, list):
                    unsupported("agent message content")
                    return result
                for i, part in enumerate(content):
                    typ = part.get("type") if isinstance(part, dict) else None
                    if not isinstance(typ, str):
                        unsupported("content part")
                    elif typ in {"input_text", "output_text"}:
                        emit("unknown", "message", part.get("text"), _pointer("payload", "content", i, "text"))
                    elif typ == "encrypted_content":
                        exclude("encrypted_payload")
                    else:
                        unsupported(typ)
            elif item_type in {"reasoning", "compaction"}:
                exclude("reasoning_or_summary")
            elif item_type == "additional_tools":
                exclude("system_or_developer")
            elif item_type == "configuration_update":
                exclude("metadata")
            else:
                unsupported(item_type)
        elif kind == "event_msg":
            event = payload.get("type")
            if not isinstance(event, str):
                unsupported("event type")
                return result
            if event in {"task_started", "task_complete", "turn_started", "turn_complete", "turn_completed"}:
                emit("unknown", "lifecycle", event, _pointer("payload", "type"))
            elif event in {"agent_reasoning", "agent_reasoning_raw_content", "agent_reasoning_section_break"}:
                exclude("reasoning")
            elif event in {"user_message", "agent_message"}:
                # Duplicated by response_item in current rollouts; cannot prove a
                # standalone event is the authoritative user turn.
                exclude("event_message_mirror")
            elif event == "item_completed":
                # The app-server's per-item view of the same turn: messages
                # repeat response_item text, and command items repeat (often
                # untruncated) the output the model was given as a call output.
                exclude("event_item_mirror")
            elif event in _CODEX_EVENT_METADATA:
                exclude("metadata")
            else:
                unsupported(event)
        elif kind in _CODEX_TOP_METADATA:
            exclude("system_or_metadata")
        elif kind in {"compacted", "retained_context"}:
            exclude("reasoning_or_summary")
        elif kind == "realtime_item":
            event = payload.get("type")
            if not isinstance(event, str):
                unsupported("realtime item type")
            elif event == "transcript_segment":
                # Speech transcript. Most segments repeat a response_item
                # message, so it stays searchable without a second citation.
                emit("unknown", "message", payload.get("text"), _pointer("payload", "text"))
            elif event in {"realtime_session_started", "realtime_session_closed", "bem_item_promoted"}:
                exclude("metadata")
            else:
                unsupported(event)
        else:
            unsupported(kind)
    else:
        unsupported("provider")
    return result


def _adapt_omp(row: dict, emit, exclude, unsupported) -> None:
    """OMP's persisted messages, including Qwen Token Plan sessions.

    Custom notifications are searchable but are not attributed to the user.
    Compaction and branch summaries are not original conversation evidence.
    """
    kind = row.get("type")
    if not isinstance(kind, str):
        unsupported("type")
        return
    if kind in {"session", "title", "title_change", "model_change", "thinking_level_change",
                "custom", "label", "session_info"}:
        exclude("system_or_metadata")
        return
    if kind in {"compaction", "branch_summary"}:
        exclude("reasoning_or_summary")
        return
    if kind == "custom_message":
        message, base, role = row, "", "unknown"
    elif kind == "message" and isinstance(row.get("message"), dict):
        message, base = row["message"], "/message"
        role = message.get("role")
        if not isinstance(role, str):
            unsupported("message role")
            return
        if role in {"system", "developer"}:
            exclude("system_or_developer")
            return
        if role == "bashExecution":
            emit("unknown", "tool_request", message.get("command"), base + "/command")
            emit("tool", "tool_result", message.get("output"), base + "/output")
            return
        if role not in {"user", "assistant", "toolResult"}:
            unsupported("message role")
            return
        role = "tool" if role == "toolResult" else role
    else:
        unsupported(kind)
        return
    channel = "tool_result" if role == "tool" else "message"
    call = message.get("toolCallId") if role == "tool" else None
    content = message.get("content")
    if isinstance(content, str):
        emit(role, channel, content, base + "/content", tool_id=call)
    elif isinstance(content, list):
        for i, part in enumerate(content):
            typ = part.get("type") if isinstance(part, dict) else None
            pointer = base + _pointer("content", i)
            if not isinstance(typ, str):
                unsupported("content part")
            elif typ == "text":
                emit(role, channel, part.get("text"), pointer + "/text", tool_id=call)
            elif typ == "toolCall" and role == "assistant":
                emit("assistant", "tool_request", part.get("name"), pointer + "/name",
                     tool_id=part.get("id"))
                arguments = part.get("arguments")
                if isinstance(arguments, dict) and isinstance(arguments.get("command"), str):
                    emit("assistant", "tool_request", arguments["command"], pointer + "/arguments/command",
                         tool_id=part.get("id"))
            elif typ in {"thinking", "redacted_thinking", "image", "audio", "document"}:
                exclude("reasoning_or_binary")
            else:
                unsupported(typ)
    else:
        unsupported("message content")
    if isinstance(message.get("errorMessage"), str):
        emit("unknown", "lifecycle", message["errorMessage"], base + "/errorMessage")


def _adapt_grok(row: dict, emit, exclude, unsupported) -> None:
    if row.get("method") not in _GROK_METHODS:
        unsupported(row.get("method"))
        return
    params = row.get("params")
    update = params.get("update") if isinstance(params, dict) else None
    event = update.get("sessionUpdate") if isinstance(update, dict) else None
    if not isinstance(event, str):
        unsupported("session update")
        return
    base = ("params", "update")
    call = update.get("toolCallId")
    if event in {"user_message_chunk", "agent_message_chunk"}:
        content = update.get("content")
        kind = content.get("type") if isinstance(content, dict) else None
        if kind == "image":
            exclude("binary_payload")
        elif kind != "text":
            unsupported(f"{event} content")
        elif event == "agent_message_chunk":
            emit("assistant", "message", content.get("text"), _pointer(*base, "content", "text"))
        elif isinstance(content.get("text"), str) and content["text"].lstrip().startswith(_SYNTHETIC_USER):
            exclude("synthetic_user_wrapper")
        else:
            emit("user", "message", content.get("text"), _pointer(*base, "content", "text"))
    elif event == "tool_call":
        meta = update.get("_meta")
        tool = meta.get("x.ai/tool") if isinstance(meta, dict) else None
        if isinstance(tool, dict) and isinstance(tool.get("name"), str):
            emit("assistant", "tool_request", tool["name"], _pointer(*base, "_meta", "x.ai/tool", "name"),
                 tool_id=call)
        else:
            emit("assistant", "tool_request", update.get("title"), _pointer(*base, "title"), tool_id=call)
        raw = update.get("rawInput")
        if isinstance(raw, dict) and isinstance(raw.get("command"), str):
            emit("assistant", "tool_request", raw["command"], _pointer(*base, "rawInput", "command"),
                 tool_id=call)
    elif event == "tool_call_update":
        finished = update.get("status") in {"completed", "failed"}
        content = update.get("content")
        if content is None:
            output = update.get("rawOutput")
            detail = output.get("output") if isinstance(output, dict) and output.get("type") == "MCP" else None
            if isinstance(detail, str):
                emit("tool", "tool_result", detail, _pointer(*base, "rawOutput", "output"), tool_id=call)
            elif isinstance(detail, dict) and any(isinstance(v, str) for v in detail.values()):
                for key, value in detail.items():
                    if isinstance(value, str):
                        emit("tool", "tool_result", value, _pointer(*base, "rawOutput", "output", key),
                             tool_id=call)
            elif output is not None:
                exclude("nontext_tool_payload")
            else:
                # A progress row repeating the call's title and input.
                exclude("metadata")
            return
        if not isinstance(content, list):
            unsupported("tool update content")
            return
        for i, item in enumerate(content):
            kind = item.get("type") if isinstance(item, dict) else None
            inner = item.get("content") if kind == "content" else None
            inner_kind = inner.get("type") if isinstance(inner, dict) else None
            if kind == "content" and inner_kind == "text":
                # Before completion the text is the caller's description of the call.
                emit("tool" if finished else "assistant", "tool_result" if finished else "tool_request",
                     inner.get("text"), _pointer(*base, "content", i, "content", "text"), tool_id=call)
            elif kind == "content" and inner_kind == "image" or kind == "diff":
                exclude("nontext_tool_payload")
            else:
                unsupported(f"tool update {kind}")
    elif event == "turn_completed":
        emit("unknown", "lifecycle", event, _pointer(*base, "sessionUpdate"))
    elif event in _GROK_SUMMARIES:
        exclude("reasoning_or_summary")
    elif event in _GROK_METADATA:
        exclude("metadata")
    else:
        unsupported(event)
