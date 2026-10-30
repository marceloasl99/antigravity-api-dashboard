import json
import os
import time

from conftest import HOMES, sse_events
import api_service_multi as svc

TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


def chat(client, content, **extra):
    body = {"model": "gemini-3.8-flash-low", "messages": [{"role": "user", "content": content}], **extra}
    return client.post("/v1/chat/completions", json=body)


# -- basic completions -------------------------------------------------------

def test_non_streaming_chat(client, config):
    r = chat(client, "hello")
    assert r.status_code == 200
    data = r.get_json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello from fake agy (home-p1)"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] == {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107}
    assert r.headers["X-Profile-Used"] == "p1"
    assert r.headers["X-Model-Used"] == "gemini-3.8-flash-low"


def test_streaming_chat(client, config):
    r = chat(client, "hello", stream=True)
    assert r.status_code == 200
    assert r.mimetype == "text/event-stream"
    events = sse_events(r.get_data(as_text=True))
    assert events[-1] == "[DONE]"
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events[:-1])
    assert text == "Hello from fake agy (home-p1)"
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[-2]["usage"]["total_tokens"] == 107


def test_legacy_completions(client, config):
    r = client.post("/v1/completions", json={"prompt": "hello"})
    assert r.status_code == 200
    assert r.get_json()["choices"][0]["text"] == "Hello from fake agy (home-p1)"


def test_responses_alias_with_input(client, config):
    r = client.post("/v1/responses", json={"input": "hello"})
    assert r.status_code == 200


def test_missing_messages_is_400(client, config):
    r = client.post("/v1/chat/completions", json={})
    assert r.status_code == 400
    assert r.get_json()["error"]["type"] == "invalid_request_error"


# -- tool calling ------------------------------------------------------------

def test_tool_calling_non_streaming(client, config):
    r = chat(client, "TOOLCALL", tools=TOOLS)
    assert r.status_code == 200
    msg = r.get_json()["choices"][0]["message"]
    assert r.get_json()["choices"][0]["finish_reason"] == "tool_calls"
    assert msg["content"] == "Let me check the weather."
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"city": "Lisbon"}


def test_tool_calling_streaming(client, config):
    r = chat(client, "TOOLCALL", tools=TOOLS, stream=True)
    events = sse_events(r.get_data(as_text=True))
    deltas = [e["choices"][0]["delta"] for e in events if e != "[DONE]"]
    text = "".join(d.get("content", "") for d in deltas)
    assert text == "Let me check the weather. "
    assert "<tool_call" not in text
    tc = [d["tool_calls"][0] for d in deltas if "tool_calls" in d]
    assert tc[0]["function"]["name"] == "get_weather" and tc[0]["id"].startswith("call_")
    assert json.loads(tc[1]["function"]["arguments"]) == {"city": "Lisbon"}
    assert events[-2]["choices"][0]["finish_reason"] == "tool_calls"


def test_two_tool_calls(client, config):
    r = chat(client, "TWO_CALLS", tools=TOOLS)
    calls = r.get_json()["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["a", "b"]


def test_tool_result_round_trip_reaches_prompt(client, config):
    body = {"tools": TOOLS, "messages": [
        {"role": "user", "content": "ECHO_PROMPT weather?"},
        {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function",
                                              "function": {"name": "get_weather", "arguments": '{"city": "Lisbon"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "22C"},
    ]}
    r = client.post("/v1/chat/completions", json=body)
    prompt = r.get_json()["choices"][0]["message"]["content"]
    assert "external tool protocol" in prompt and "get_weather" in prompt
    assert '<tool_result id="call_1">\n22C\n</tool_result>' in prompt


def test_without_tools_tags_pass_through(client, config):
    r = chat(client, "TOOLCALL")
    msg = r.get_json()["choices"][0]["message"]
    assert "tool_calls" not in msg
    assert "<tool_call>" in msg["content"]


# -- effort resolution -------------------------------------------------------

def test_effort_from_model_suffix_wins(client, config):
    config(active_effort="low")
    r = client.post("/v1/chat/completions", json={"model": "gemini-3.8-flash-high", "messages": [{"role": "user", "content": "ECHO_EFFORT"}]})
    assert r.get_json()["choices"][0]["message"]["content"] == "high"
    assert r.headers["X-Model-Used"] == "gemini-3.8-flash-high"


def test_non_gemini_models_pass_through_without_effort(client, config):
    config(active_effort="high")
    for requested, expected in [("claude-opus-4-6-thinking", "claude-opus-4-6-thinking"),
                                ("claude-sonnet-4-5-20250929", "claude-sonnet-4-6"),
                                ("claude-opus-4-1", "claude-opus-4-6-thinking"),
                                ("gpt-oss-120b-medium", "gpt-oss-120b-medium"),
                                ("gpt-4o", "gemini-3.8-flash-high")]:
        r = client.post("/v1/chat/completions", json={"model": requested, "messages": [{"role": "user", "content": "ECHO_MODEL"}]})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["choices"][0]["message"]["content"] == expected, requested
        assert r.get_json()["model"] == requested   # echoed back so client validators match


def test_models_endpoint_lists_all_cli_models(client, config):
    ids = {m["id"] for m in client.get("/v1/models").get_json()["data"]}
    assert {"gemini-3.8-flash-low", "gemini-3.8-flash-high", "claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium"} <= ids


def test_effort_from_config_when_no_suffix(client, config):
    config(active_effort="medium")
    r = client.post("/v1/chat/completions", json={"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "ECHO_EFFORT"}]})
    assert r.get_json()["choices"][0]["message"]["content"] == "medium"


def test_effort_auto_falls_back_to_low(client, config):
    config(active_effort="auto")
    r = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "ECHO_EFFORT"}]})
    assert r.get_json()["choices"][0]["message"]["content"] == "low"
    r = client.post("/v1/chat/completions", json={"model": "gemini-3.8-flash-medium", "messages": [{"role": "user", "content": "ECHO_EFFORT"}]})
    assert r.get_json()["choices"][0]["message"]["content"] == "medium"


def test_internal_tools_flag_controls_permissions(client, config):
    r = chat(client, "ECHO_TOOLS")
    assert r.get_json()["choices"][0]["message"]["content"] == "tools:on"
    config(agy_internal_tools=False)
    r = chat(client, "ECHO_TOOLS")
    assert r.get_json()["choices"][0]["message"]["content"] == "tools:off"


# -- routing: direct / round robin / fallback --------------------------------

def test_single_account_is_direct(client, config):
    used = {chat(client, "hi").headers["X-Profile-Used"] for _ in range(4)}
    assert used == {"p1"}
    assert client.get("/v1/config").get_json()["routing_mode"] == "direct"


def test_two_accounts_round_robin(client, config):
    config(enabled_profiles=["p1", "p3"])
    assert client.get("/v1/config").get_json()["routing_mode"] == "round_robin"
    used = [chat(client, "hi").headers["X-Profile-Used"] for _ in range(6)]
    assert set(used) == {"p1", "p3"}
    assert used[0] != used[1] and used[1] != used[2]  # alternates
    assert used.count("p1") == 3


def test_three_accounts_round_robin(client, config):
    config(enabled_profiles=["p1", "p2", "p3"])
    used = [chat(client, "hi").headers["X-Profile-Used"] for _ in range(6)]
    assert sorted(used[:3]) == ["p1", "p2", "p3"]
    assert used[:3] == used[3:]


def test_fallback_to_next_enabled_account(client, config, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_FAIL_HOMES", "home-p1")
    config(enabled_profiles=["p1", "p2"])
    used = [chat(client, "hi").headers["X-Profile-Used"] for _ in range(4)]
    assert set(used) == {"p2"}
    assert svc.PROFILE_INDEX["p1"]["stats"]["errors"] >= 1


def test_disabled_account_never_used_even_on_failure(client, config, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_FAIL_HOMES", "home-p1")
    config(enabled_profiles=["p1"])
    r = chat(client, "hi")
    assert r.status_code == 502
    assert r.get_json()["error"]["code"] == "upstream_error"
    assert "simulated failure" in r.get_json()["error"]["message"]


def test_crash_on_start_falls_back(client, config, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_CRASH_HOMES", "home-p1")
    config(enabled_profiles=["p1", "p2"])
    r = chat(client, "hi")
    assert r.status_code == 200
    assert r.headers["X-Profile-Used"] == "p2"


def test_streaming_error_is_reported_in_stream(client, config):
    r = chat(client, "FAIL", stream=True)
    events = sse_events(r.get_data(as_text=True))
    assert any("error" in e for e in events if e != "[DONE]")
    assert events[-1] == "[DONE]"


# -- config endpoint ---------------------------------------------------------

def test_config_post_enabled_profiles_and_effort(client, config):
    r = client.post("/v1/config", json={"enabled_profiles": ["p2", "p3"], "active_effort": "auto"})
    assert r.status_code == 200
    cfg = r.get_json()["config"]
    assert cfg["enabled_profiles"] == ["p2", "p3"]
    assert cfg["routing_mode"] == "round_robin"
    assert cfg["active_effort"] == "auto"
    assert cfg["active_model"] == "gemini-3.8-flash"
    assert "auto" in cfg["effort_options"]


def test_config_post_legacy_active_profile(client, config):
    config(enabled_profiles=["p1", "p2"])
    r = client.post("/v1/config", json={"active_profile": "p3"})
    assert r.get_json()["config"]["enabled_profiles"] == ["p3"]
    assert r.get_json()["config"]["routing_mode"] == "direct"


def test_config_rejects_bad_values(client, config):
    assert client.post("/v1/config", json={"enabled_profiles": []}).status_code == 400
    assert client.post("/v1/config", json={"enabled_profiles": ["zz"]}).status_code == 400
    assert client.post("/v1/config", json={"active_effort": "ultra"}).status_code == 400
    assert svc.get_config()["enabled_profiles"] == ["p1"]


def test_config_file_persisted_and_normalized(client, config):
    with open(svc.CONFIG_FILE, "w") as f:
        json.dump({"active_profile": "p2", "active_effort": "HIGH"}, f)
    os.utime(svc.CONFIG_FILE, (time.time() + 5, time.time() + 5))  # force mtime change
    cfg = svc.get_config()
    assert cfg["enabled_profiles"] == ["p2"] and cfg["active_effort"] == "high"


# -- other endpoints ---------------------------------------------------------

def test_models_and_health(client, config):
    ids = [m["id"] for m in client.get("/v1/models").get_json()["data"]]
    assert "gemini-3.8-flash-high" in ids
    h = client.get("/v1/health").get_json()
    assert h["status"] == "ok" and "pool" in h and h["routing_mode"] == "direct"


def test_profiles_and_usage(client, config):
    p = client.get("/v1/profiles").get_json()
    assert [x["enabled"] for x in p["profiles"]] == [True, False, False]
    u = client.get("/v1/profiles/p1/usage").get_json()
    assert u["ok"] and u["groups"][0]["name"] == "weekly"


def test_token_tracker_records(client, config):
    before = client.get("/v1/tokens/usage").get_json()["all_time"]["requests"]
    chat(client, "hi")
    after = client.get("/v1/tokens/usage").get_json()
    assert after["all_time"]["requests"] == before + 1
    assert after["recent_requests"][0]["profile"] == "p1"
    assert after["recent_requests"][0]["total_tokens"] == 107
