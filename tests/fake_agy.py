"""
Fake Antigravity CLI for tests. Speaks the documented headless protocol:

  argv : --input-format stream-json --output-format stream-json --model M [--effort E] [--dangerously-skip-permissions]
  stdout: {"event":"init",...} then per stdin line: step_update* + result

Behaviour hooks (via prompt text):
  ECHO_PROMPT   -> response is the full prompt received
  ECHO_EFFORT   -> response is the --effort value (or "none")
  ECHO_PID      -> response is this process' pid
  ECHO_TOOLS    -> "tools:on" / "tools:off" (--dangerously-skip-permissions present?)
  TOOLCALL      -> text + one <tool_call> block, streamed in awkward chunks
  TWO_CALLS     -> two <tool_call> blocks
  FAIL          -> result with status ERROR
  SLOW          -> sleeps FAKE_AGY_SLOW seconds before answering
Environment hooks:
  FAKE_AGY_INIT_DELAY  seconds to wait before emitting init (simulated cold start)
  FAKE_AGY_FAIL_HOMES  comma-separated substrings; if HOME matches, every turn fails
  FAKE_AGY_CRASH_HOMES comma-separated substrings; if HOME matches, exit 1 before init
Also supports `--print /usage --output-format json` for the quota endpoint.
"""

import json
import os
import sys
import time
import uuid


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    home = os.environ.get("HOME", "")

    if "--print" in argv:
        out({"command": {"data": {"description": "fake quota", "groups": [
            {"name": "weekly", "used_fraction": 0.25}, {"name": "5h", "used_fraction": 0.1}]}}})
        return 0

    model = argv[argv.index("--model") + 1] if "--model" in argv else "gemini-3.8-flash-low"
    if "--effort" in argv:  # agy 1.4.10 rejects --effort together with suffixed/non-Gemini ids
        out({"event": "result", "result": {"status": "ERROR", "error": f'invalid model selection (--model "{model}" --effort)'}})
        return 1
    effort = model.rsplit("-", 1)[-1] if model.startswith("gemini") else "none"
    tools_on = "--dangerously-skip-permissions" in argv
    agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
    if agent and not os.path.isfile(os.path.join(home, ".gemini", "config", "agents", f"{agent}.md")):
        sys.stderr.write(f"agent not found: {agent}\n")
        return 1

    for frag in filter(None, os.environ.get("FAKE_AGY_CRASH_HOMES", "").split(",")):
        if frag in home:
            sys.stderr.write("authentication required\n")
            return 1

    time.sleep(float(os.environ.get("FAKE_AGY_INIT_DELAY", "0")))
    conv = uuid.uuid4().hex
    out({"event": "init", "conversation_id": conv,
         "init": {"cwd": os.getcwd(), "tools": ["run_command"], "permission_mode": "always-proceed" if tools_on else "request-review"}})

    fail_home = any(frag in home for frag in filter(None, os.environ.get("FAKE_AGY_FAIL_HOMES", "").split(",")))
    step = 0

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            out({"event": "result", "result": {"conversation_id": conv, "status": "ERROR", "error": "invalid json"}})
            return 1
        if msg.get("event") != "user":
            sys.stderr.write(f'warning: ignoring unsupported stream input message event "{msg.get("event")}"\n')
            continue
        content = msg.get("message", {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))

        if "SLOW" in content:
            time.sleep(float(os.environ.get("FAKE_AGY_SLOW", "2")))

        if fail_home or "FAIL" in content:
            out({"event": "result", "result": {"conversation_id": conv, "status": "ERROR",
                                               "error": f"simulated failure for {os.path.basename(home) or 'home'}"}})
            continue

        if "ECHO_PROMPT" in content:
            chunks = [content]
        elif "ECHO_EFFORT" in content:
            chunks = [effort]
        elif "ECHO_MODEL" in content:
            chunks = [model]
        elif "ECHO_AGENT" in content:
            chunks = [agent or "default"]
        elif "ECHO_PID" in content:
            chunks = [str(os.getpid())]
        elif "ECHO_TOOLS" in content:
            chunks = ["tools:on" if tools_on else "tools:off"]
        elif "TWO_CALLS" in content:
            chunks = ['<tool_call>{"name": "a", "arguments": {"x": 1}}</tool_call>\n',
                      '<tool_call>{"name": "b", "arguments": {"y": "z"}}</tool_call>']
        elif "TOOLCALL" in content:
            # Split so the opening tag straddles deltas: "<tool_c" + "all>..."
            chunks = ["Let me check the weather. ", "<tool_c", 'all>{"name": "get_weather", "argu',
                      'ments": {"city": "Lisbon"}}</tool_', "call>"]
        else:
            chunks = ["Hello ", "from ", f"fake agy ({os.path.basename(home) or 'home'})"]

        for ch in chunks:
            step += 1
            out({"event": "step_update", "step_update": {"conversation_id": conv, "step_index": step,
                                                         "state": "ACTIVE", "step_type": "agent_response", "text_delta": ch}})
        full = "".join(chunks)
        out({"event": "step_update", "step_update": {"conversation_id": conv, "step_index": step, "state": "DONE",
                                                     "step_type": "agent_response"}})
        out({"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS", "response": full,
                                           "duration_seconds": 0.01, "num_turns": 1,
                                           "usage": {"input_tokens": 100, "output_tokens": 7, "thinking_tokens": 0,
                                                     "cache_read_tokens": 0, "total_tokens": 107}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
