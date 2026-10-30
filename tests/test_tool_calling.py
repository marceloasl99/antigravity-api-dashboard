import json

from tool_calling import ToolCallParser, build_prompt, parse_complete

TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Weather by city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]


def test_parser_handles_tag_split_across_deltas():
    p = ToolCallParser(True)
    emitted = ""
    for d in ["Let me check. ", "<tool_c", 'all>{"name": "get_weather", "argu', 'ments": {"city": "Lisbon"}}</tool_', "call>"]:
        emitted += p.feed(d)
    emitted += p.finish()
    assert emitted == "Let me check. "
    assert len(p.tool_calls) == 1
    call = p.tool_calls[0]
    assert call["type"] == "function"
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Lisbon"}


def test_parser_multiple_calls_and_trailing_text():
    text, calls = parse_complete('<tool_call>{"name":"a","arguments":{"x":1}}</tool_call>\n'
                                 '<tool_call>{"name":"b","arguments":{"y":"z"}}</tool_call> done')
    assert [c["function"]["name"] for c in calls] == ["a", "b"]
    assert text == "done"


def test_parser_disabled_passes_text_through():
    text, calls = parse_complete('<tool_call>{"name":"a"}</tool_call>', enabled=False)
    assert calls == []
    assert text == '<tool_call>{"name":"a"}</tool_call>'


def test_parser_tolerates_fenced_json_and_string_arguments():
    text, calls = parse_complete('<tool_call>```json\n{"name":"a","arguments":"{\\"k\\": 2}"}\n```</tool_call>')
    assert json.loads(calls[0]["function"]["arguments"]) == {"k": 2}
    assert text == ""


def test_parser_false_alarm_angle_bracket_is_emitted():
    p = ToolCallParser(True)
    out = p.feed("a < b and <tool") + p.feed("box> c")
    out += p.finish()
    assert out == "a < b and <toolbox> c"
    assert p.tool_calls == []


def test_build_prompt_includes_tools_and_protocol():
    prompt = build_prompt([{"role": "user", "content": "weather?"}], TOOLS)
    assert "get_weather" in prompt
    assert "<tool_call>" in prompt
    assert "\n\nweather?\n\n" in prompt                       # single turn: no transcript framing, no role label
    assert not prompt.startswith("Below is the conversation so far")
    assert prompt.endswith("otherwise just answer the user.")
    multi = build_prompt([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                          {"role": "user", "content": "weather?"}], TOOLS)
    assert multi.startswith("Below is the conversation so far")
    assert "\n\nUser: hi\n\nAssistant: hello\n\nUser: weather?\n\n" in multi


def test_build_prompt_renders_tool_round_trip():
    msgs = [
        {"role": "user", "content": "weather in Lisbon?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Lisbon"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "22C sunny"},
    ]
    prompt = build_prompt(msgs, TOOLS)
    assert 'Assistant: <tool_call>{"id": "call_1", "name": "get_weather", "arguments": {"city": "Lisbon"}}</tool_call>' in prompt
    assert '<tool_result id="call_1" name="get_weather">\n22C sunny\n</tool_result>' in prompt


def test_build_prompt_tool_choice_none_omits_tools():
    prompt = build_prompt([{"role": "user", "content": "hi"}], TOOLS, tool_choice="none")
    assert prompt == "hi"


def test_build_prompt_forced_tool_choice():
    prompt = build_prompt([{"role": "user", "content": "hi"}], TOOLS,
                          tool_choice={"type": "function", "function": {"name": "get_weather"}})
    assert 'You MUST call the tool "get_weather" now.' in prompt
