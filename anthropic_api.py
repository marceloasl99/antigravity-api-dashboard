"""
Anthropic Messages API (/v1/messages) on top of the same agy execution path.

This is what Claude Code speaks (ANTHROPIC_BASE_URL). Supported:
  * system (string or blocks), messages with text / tool_use / tool_result blocks
  * tools (name / description / input_schema), tool_choice auto|any|tool|none
  * non-streaming and streaming (message_start, content_block_*, message_delta, message_stop)
  * /v1/messages/count_tokens (estimate)
Everything is translated to the OpenAI-style message list used by tool_calling.build_prompt,
so tool calling works the same way as on /v1/chat/completions.
"""

import json
import time
import uuid

from flask import Response, jsonify, request, stream_with_context

from tool_calling import ToolCallParser, build_prompt


def _msg_id():
    return "msg_" + uuid.uuid4().hex[:24]


def _tool_use_id():
    return "toolu_" + uuid.uuid4().hex[:24]


def _blocks_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(p for p in parts if p)


def anthropic_to_openai(data: dict):
    """Return (messages, tools, tool_choice) in OpenAI shape."""
    messages = []
    system = data.get("system")
    if system:
        text = _blocks_text(system)
        if text:
            messages.append({"role": "system", "content": text})

    for m in data.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        texts, tool_calls = [], []
        for b in content or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_use" and role == "assistant":
                tool_calls.append({"id": b.get("id") or _tool_use_id(), "type": "function",
                                   "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
            elif t == "tool_result":
                # flush any text gathered so far as its own message to keep ordering
                if texts:
                    messages.append({"role": role, "content": "\n".join(texts)})
                    texts = []
                messages.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                                 "content": _blocks_text(b.get("content")) + (" [error]" if b.get("is_error") else "")})
            elif t == "image":
                texts.append("[image omitted]")
        if texts or tool_calls:
            msg = {"role": role, "content": "\n".join(texts) if texts else None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)

    tools = [{"type": "function", "function": {"name": t.get("name"), "description": t.get("description", ""),
                                               "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
             for t in data.get("tools") or [] if isinstance(t, dict) and t.get("name")]

    tc = data.get("tool_choice")
    tool_choice = None
    if isinstance(tc, dict):
        kind = tc.get("type")
        if kind == "any":
            tool_choice = "required"
        elif kind == "tool":
            tool_choice = {"type": "function", "function": {"name": tc.get("name")}}
        elif kind == "none":
            tool_choice = "none"
    if not tools:
        tool_choice = "none"
    return messages, tools, tool_choice


def _tool_use_block(call: dict) -> dict:
    try:
        inp = json.loads(call["function"]["arguments"] or "{}")
    except json.JSONDecodeError:
        inp = {"input": call["function"]["arguments"]}
    return {"type": "tool_use", "id": _tool_use_id(), "name": call["function"]["name"], "input": inp}


def _error(message: str, status: int = 502, etype: str = "api_error"):
    return jsonify({"type": "error", "error": {"type": etype, "message": message}}), status


def _sse(event: str, obj: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n"


def register(app, svc):
    """Mount the Anthropic-compatible routes onto `app`, using service module `svc`."""

    @app.route("/v1/messages", methods=["POST", "OPTIONS"])
    def anthropic_messages():
        if request.method == "OPTIONS":
            return ("", 204)
        data = request.get_json(force=True, silent=True) or {}
        messages, tools, tool_choice = anthropic_to_openai(data)
        if not messages:
            return _error("messages: at least one message is required", 400, "invalid_request_error")

        cfg = svc.get_config()
        requested_model = data.get("model") or cfg["active_model"]
        model = svc.resolve_model(requested_model, cfg)
        profiles = svc.get_target_profiles(cfg)
        tools_enabled = tool_choice != "none"
        prompt = build_prompt(messages, tools, tool_choice)
        mid = _msg_id()

        if data.get("stream"):
            return _stream(prompt, model, requested_model, profiles, cfg["agy_internal_tools"], tools_enabled, mid)

        parser = ToolCallParser(tools_enabled)
        done = None
        try:
            for kind, payload in svc.run_turn(prompt, model, profiles, cfg["agy_internal_tools"]):
                if kind == "delta":
                    parser.feed(payload)
                elif kind == "done":
                    done = payload
                elif kind == "error":
                    return _error(payload)
        except svc.AgyError as e:
            return _error(str(e))
        parser.finish()

        content = []
        text = parser.text.strip()
        if text:
            content.append({"type": "text", "text": text})
        for call in parser.tool_calls:
            content.append(_tool_use_block(call))
        if not content:
            content.append({"type": "text", "text": ""})
        usage = done["usage"]
        resp = jsonify({
            "id": mid, "type": "message", "role": "assistant", "model": requested_model,
            "content": content,
            "stop_reason": "tool_use" if parser.tool_calls else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]},
        })
        resp.headers["X-Profile-Used"] = done["profile"]["id"]
        resp.headers["X-Model-Used"] = model
        return resp

    def _stream(prompt, model, requested_model, profiles, internal_tools, tools_enabled, mid):
        def generate():
            parser = ToolCallParser(tools_enabled)
            yield _sse("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": requested_model, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
            index = -1
            text_open = False
            done = None
            try:
                for kind, payload in svc.run_turn(prompt, model, profiles, internal_tools):
                    if kind == "delta":
                        text = parser.feed(payload)
                        if text:
                            if not text_open:
                                index += 1
                                text_open = True
                                yield _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
                            yield _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}})
                    elif kind == "done":
                        done = payload
                    elif kind == "error":
                        yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": payload}})
                        return
            except svc.AgyError as e:
                yield _sse("error", {"type": "error", "error": {"type": "api_error", "message": str(e)}})
                return

            tail = parser.finish()
            if tail:
                if not text_open:
                    index += 1
                    text_open = True
                    yield _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": tail}})
            if text_open:
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})

            for call in parser.tool_calls:
                index += 1
                block = _tool_use_block(call)
                yield _sse("content_block_start", {"type": "content_block_start", "index": index,
                                                   "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}})
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})

            out_tokens = done["usage"]["completion_tokens"] if done else 0
            in_tokens = done["usage"]["prompt_tokens"] if done else 0
            yield _sse("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": "tool_use" if parser.tool_calls else "end_turn", "stop_sequence": None},
                                         "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens}})
            yield _sse("message_stop", {"type": "message_stop"})

        return Response(stream_with_context(generate()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", "X-Model-Used": model})

    @app.route("/v1/messages/count_tokens", methods=["POST", "OPTIONS"])
    def anthropic_count_tokens():
        if request.method == "OPTIONS":
            return ("", 204)
        data = request.get_json(force=True, silent=True) or {}
        messages, tools, tool_choice = anthropic_to_openai(data)
        prompt = build_prompt(messages, tools, tool_choice) if messages else ""
        return jsonify({"input_tokens": max(1, len(prompt) // 4)})
