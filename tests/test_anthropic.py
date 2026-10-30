import json

from anthropic_api import anthropic_to_openai

TOOLS = [{"name": "get_weather", "description": "Weather by city",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]


def events(body: str):
    out = []
    for block in body.split("\n\n"):
        lines = [l for l in block.strip().splitlines() if l]
        if len(lines) >= 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def test_translation_round_trip():
    msgs, tools, choice = anthropic_to_openai({
        "system": [{"type": "text", "text": "Be brief."}],
        "messages": [
            {"role": "user", "content": "weather in Lisbon?"},
            {"role": "assistant", "content": [{"type": "text", "text": "Checking."},
                                              {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Lisbon"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "22C"}]}]},
        ],
        "tools": TOOLS, "tool_choice": {"type": "any"},
    })
    assert msgs[0] == {"role": "system", "content": "Be brief."}
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": '{"city": "Lisbon"}'}
    assert msgs[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "22C"}
    assert tools[0]["function"]["parameters"]["required"] == ["city"]
    assert choice == "required"


def test_messages_non_streaming_text(client, config):
    r = client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "max_tokens": 100,
                                          "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    d = r.get_json()
    assert d["type"] == "message" and d["role"] == "assistant"
    assert d["content"] == [{"type": "text", "text": "Hello from fake agy (home-p1)"}]
    assert d["stop_reason"] == "end_turn"
    assert d["usage"] == {"input_tokens": 100, "output_tokens": 7}
    assert r.headers["X-Model-Used"] == "claude-sonnet-4-6"


def test_messages_model_aliases(client, config):
    for requested, expected in [("claude-opus-4-1-20250805", "claude-opus-4-6-thinking"),
                                ("claude-3-5-haiku-20241022", "gemini-3.8-flash-low"),
                                ("claude-sonnet-4-5", "claude-sonnet-4-6")]:
        r = client.post("/v1/messages", json={"model": requested, "max_tokens": 10, "messages": [{"role": "user", "content": "ECHO_MODEL"}]})
        assert r.get_json()["content"][0]["text"] == expected, requested
        assert r.get_json()["model"] == requested


def test_messages_tool_use(client, config):
    r = client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "max_tokens": 100, "tools": TOOLS,
                                          "messages": [{"role": "user", "content": "TOOLCALL"}]})
    d = r.get_json()
    assert d["stop_reason"] == "tool_use"
    assert d["content"][0] == {"type": "text", "text": "Let me check the weather."}
    tu = d["content"][1]
    assert tu["type"] == "tool_use" and tu["id"].startswith("toolu_")
    assert tu["name"] == "get_weather" and tu["input"] == {"city": "Lisbon"}


def test_messages_tool_result_reaches_prompt(client, config):
    r = client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "max_tokens": 100, "tools": TOOLS, "messages": [
        {"role": "user", "content": "ECHO_PROMPT weather?"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_9", "name": "get_weather", "input": {"city": "Lisbon"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_9", "content": "22C"}]}]})
    content = r.get_json()["content"]
    assert '<tool_result id="toolu_9">\n22C\n</tool_result>' in content[0]["text"]
    # the echoed prompt contained the prior <tool_call> block, which the parser turns back into a tool_use
    assert content[1]["type"] == "tool_use" and content[1]["name"] == "get_weather" and content[1]["input"] == {"city": "Lisbon"}


def test_messages_streaming_text(client, config):
    r = client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "max_tokens": 100, "stream": True,
                                          "messages": [{"role": "user", "content": "hello"}]})
    assert r.mimetype == "text/event-stream"
    ev = events(r.get_data(as_text=True))
    names = [e[0] for e in ev]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    assert names[1] == "content_block_start" and ev[1][1]["content_block"]["type"] == "text"
    text = "".join(e[1]["delta"]["text"] for e in ev if e[0] == "content_block_delta")
    assert text == "Hello from fake agy (home-p1)"
    delta = [e[1] for e in ev if e[0] == "message_delta"][0]
    assert delta["delta"]["stop_reason"] == "end_turn" and delta["usage"]["output_tokens"] == 7


def test_messages_streaming_tool_use(client, config):
    r = client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "max_tokens": 100, "stream": True, "tools": TOOLS,
                                          "messages": [{"role": "user", "content": "TOOLCALL"}]})
    ev = events(r.get_data(as_text=True))
    starts = [e[1]["content_block"] for e in ev if e[0] == "content_block_start"]
    assert starts[0]["type"] == "text" and starts[1]["type"] == "tool_use" and starts[1]["name"] == "get_weather"
    partial = "".join(e[1]["delta"]["partial_json"] for e in ev if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "input_json_delta")
    assert json.loads(partial) == {"city": "Lisbon"}
    text = "".join(e[1]["delta"]["text"] for e in ev if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta")
    assert "<tool_call" not in text
    assert [e[1] for e in ev if e[0] == "message_delta"][0]["delta"]["stop_reason"] == "tool_use"


def test_messages_error_shape(client, config):
    r = client.post("/v1/messages", json={"model": "claude-sonnet-4-6", "max_tokens": 10, "messages": [{"role": "user", "content": "FAIL"}]})
    assert r.status_code == 502
    assert r.get_json()["type"] == "error" and r.get_json()["error"]["type"] == "api_error"


def test_count_tokens(client, config):
    r = client.post("/v1/messages/count_tokens", json={"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "x" * 400}]})
    assert r.status_code == 200 and r.get_json()["input_tokens"] >= 100
