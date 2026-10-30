"""
OpenAI-style tool calling over a text-only CLI.

agy has no way to receive tool definitions or tool results (its own tools run
inside the agent), so we implement function calling as a prompt protocol:

  * Tool definitions are rendered into the prompt (JSON schemas).
  * The model requests a call by emitting
        <tool_call>{"name": "...", "arguments": {...}}</tool_call>
    (one block per call, nothing else after the blocks).
  * The gateway parses those blocks (also mid-stream) and returns them as
    OpenAI `tool_calls`; the client executes the tool and replies with a
    `role: "tool"` message, which we render back as <tool_result>.
"""

import json
import re
import uuid

TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def make_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:24]


# ---------------------------------------------------------------------------
# Message content helpers
# ---------------------------------------------------------------------------

def extract_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif "text" in item:
                    parts.append(str(item["text"]))
        return " ".join(parts).strip()
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", "")).strip()
        return str(content.get("text", "") or content.get("content", "")).strip()
    return str(content).strip()


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _normalize_tools(tools) -> list:
    """Accept OpenAI `tools` (type=function) and legacy `functions`."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" or "function" in t else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def tools_system_block(tools: list, tool_choice=None) -> str:
    specs = _normalize_tools(tools)
    if not specs:
        return ""
    names = ", ".join(s["name"] for s in specs)
    lines = [
        "# IMPORTANT: external tool protocol for this session",
        "In this session you are a model behind an API. You have NO built-in tools: no file access, no shell, no browser,",
        "no web search. Do not try to use any such capability and do not pretend to have used one.",
        f"The ONLY tools that exist are these, and they are executed by the CLIENT application: {names}.",
        "",
        "Tool definitions (JSON schema):",
    ]
    for s in specs:
        lines.append(json.dumps(s, ensure_ascii=False))
    lines += [
        "",
        "Whenever a tool would help answer the user (reading files, fetching data, running anything), you MUST request it",
        "by writing one block per call, EXACTLY in this format, as the LAST thing in your reply:",
        '<tool_call>{"name": "<tool name>", "arguments": {<json arguments matching the schema>}}</tool_call>',
        "Rules: raw block, no markdown fences; nothing after the blocks; several blocks are allowed; never invent results.",
        "The client then replies with <tool_result> blocks containing the real output; use them to answer.",
        "If the user's request needs none of these tools, answer normally with no <tool_call> block.",
        "",
        "Example:",
        "User: What is in config.json?",
        'Assistant: <tool_call>{"name": "read_file", "arguments": {"path": "config.json"}}</tool_call>',
    ]
    choice = tool_choice
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            lines.append(f'\nYou MUST call the tool "{name}" now.')
    elif choice == "required":
        lines.append("\nYou MUST call at least one tool now.")
    return "\n".join(lines)


def _render_assistant(msg: dict) -> str:
    text = extract_text(msg.get("content"))
    calls = msg.get("tool_calls") or []
    blocks = []
    for c in calls:
        fn = c.get("function") or {}
        args = fn.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                pass
        blocks.append(TOOL_CALL_OPEN + json.dumps(
            {"id": c.get("id"), "name": fn.get("name"), "arguments": args}, ensure_ascii=False) + TOOL_CALL_CLOSE)
    if not calls and msg.get("function_call"):
        fc = msg["function_call"]
        blocks.append(TOOL_CALL_OPEN + json.dumps(
            {"name": fc.get("name"), "arguments": fc.get("arguments", {})}, ensure_ascii=False) + TOOL_CALL_CLOSE)
    return "\n".join([p for p in [text] + blocks if p])


def _render_tool_result(msg: dict) -> str:
    content = extract_text(msg.get("content"))
    attrs = []
    if msg.get("tool_call_id"):
        attrs.append(f'id="{msg["tool_call_id"]}"')
    if msg.get("name"):
        attrs.append(f'name="{msg["name"]}"')
    head = "<tool_result" + (" " + " ".join(attrs) if attrs else "") + ">"
    return f"{head}\n{content}\n</tool_result>"


CONVERSATION_HEADER = (
    "Below is the conversation so far between the user and you (the assistant), in order. "
    "Lines starting with \"Tool result\" are the real outputs of tools you requested earlier. "
    "Write ONLY your next reply to the user, with no role label and without repeating the conversation."
)
TOOLS_REMINDER = (
    "Reminder: if answering needs one of the external tools listed above, end your reply with <tool_call> block(s); "
    "otherwise just answer the user."
)


def build_prompt(messages: list, tools=None, tool_choice=None) -> str:
    """
    Flatten an OpenAI messages array (plus tools) into one agy prompt. The CLI
    only accepts a single user message per turn, so the whole history is sent
    as a labelled transcript with an explicit framing header.
    """
    if not messages:
        return "Hello"

    tools_block = "" if tool_choice == "none" else tools_system_block(tools, tool_choice)

    if not tools_block and len(messages) == 1 and messages[0].get("role") in ("user", None):
        text = extract_text(messages[0].get("content"))
        return text or "Hello"

    systems, turns = [], []
    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        if role in ("system", "developer"):
            content = extract_text(msg.get("content"))
            if content:
                systems.append(f"System: {content}")
        elif role == "assistant":
            content = _render_assistant(msg)
            if content:
                turns.append(f"Assistant: {content}")
        elif role in ("tool", "function"):
            turns.append(f"Tool result: {_render_tool_result(msg)}")
        else:
            content = extract_text(msg.get("content"))
            if content:
                turns.append(f"User: {content}")

    # A lone user turn needs no transcript framing (it reads as a pasted log otherwise).
    multi_turn = len(turns) > 1
    parts = ([CONVERSATION_HEADER] if multi_turn else []) + systems
    if tools_block:
        parts.append(f"System: {tools_block}")
    parts += turns if multi_turn else [t.split(": ", 1)[1] if t.startswith("User: ") else t for t in turns]
    if tools_block:
        parts.append(TOOLS_REMINDER)

    result = "\n\n".join(parts).strip()
    return result or "Hello"


# ---------------------------------------------------------------------------
# Response parsing (streaming-safe)
# ---------------------------------------------------------------------------

def _parse_call(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name") or (data.get("function") or {}).get("name")
    if not name:
        return None
    args = data.get("arguments")
    if args is None:
        args = data.get("parameters") or (data.get("function") or {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    return {
        "id": data.get("id") or make_call_id(),
        "type": "function",
        "function": {"name": str(name), "arguments": json.dumps(args, ensure_ascii=False)},
    }


class ToolCallParser:
    """
    Incremental parser. feed(delta) returns plain text that is safe to stream
    to the client; completed tool calls accumulate in .tool_calls. Call
    finish() at the end to flush any trailing text.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.buffer = ""
        self.tool_calls = []
        self.text = ""

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        if not self.enabled:
            self.text += delta
            return delta
        self.buffer += delta
        out = []
        while True:
            start = self.buffer.find(TOOL_CALL_OPEN)
            if start == -1:
                # Hold back a possible partial opening tag at the tail.
                hold = _partial_suffix_len(self.buffer, TOOL_CALL_OPEN)
                emit, self.buffer = self.buffer[:len(self.buffer) - hold], self.buffer[len(self.buffer) - hold:]
                out.append(emit)
                break
            end = self.buffer.find(TOOL_CALL_CLOSE, start)
            if end == -1:
                out.append(self.buffer[:start])
                self.buffer = self.buffer[start:]
                break
            out.append(self.buffer[:start])
            call = _parse_call(self.buffer[start + len(TOOL_CALL_OPEN):end])
            if call:
                self.tool_calls.append(call)
            self.buffer = self.buffer[end + len(TOOL_CALL_CLOSE):]
        emitted = "".join(out)
        self.text += emitted
        return emitted

    def finish(self) -> str:
        if not self.enabled or not self.buffer:
            rest, self.buffer = self.buffer, ""
            self.text += rest
            return rest
        rest = self.buffer
        self.buffer = ""
        if rest.startswith(TOOL_CALL_OPEN):
            # Unterminated block: try to parse what we have, otherwise drop it.
            call = _parse_call(rest[len(TOOL_CALL_OPEN):])
            if call:
                self.tool_calls.append(call)
                return ""
        self.text += rest
        return rest


def _partial_suffix_len(s: str, tag: str) -> int:
    """Length of the longest suffix of s that is a proper prefix of tag."""
    max_len = min(len(s), len(tag) - 1)
    for n in range(max_len, 0, -1):
        if s.endswith(tag[:n]):
            return n
    return 0


def parse_complete(text: str, enabled: bool = True):
    """Non-streaming helper: returns (clean_text, tool_calls)."""
    p = ToolCallParser(enabled)
    p.feed(text)
    p.finish()
    return p.text.strip(), p.tool_calls
