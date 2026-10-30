"""
OpenAI-compatible REST API wrapper for the Antigravity CLI (agy) — v3.

  * Gemini 3.8 Flash with reasoning effort low / medium / high / auto.
  * 1..N enabled Google accounts; direct routing with one account, round robin
    with two or more, automatic fallback to the next enabled account on error.
  * Warm pool of pre-started agy processes (agy_runner.py) so requests skip
    the CLI cold start (OAuth + loadCodeAssist).
  * OpenAI tool calling (tools / tool_choice / tool_calls) via a prompt
    protocol (tool_calling.py), streaming included.
  * Upstream failures are real HTTP errors (502) in OpenAI error format.

Endpoints:
  POST /v1/chat/completions   (alias: /v1/responses)
  POST /v1/completions
  GET  /v1/models, /v1/models/{id}
  GET/POST /v1/config
  GET  /v1/profiles, /v1/profiles/usage, /v1/profiles/{id}/usage, /v1/profiles/{id}/health
  GET  /v1/tokens/usage (alias /v1/token_usage)
  GET  /v1/health
"""

import json
import os
import random
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

import agy_runner
from agy_runner import AgyError, WarmPool
from tool_calling import ToolCallParser, build_prompt, extract_text
from token_tracker import get_token_usage_summary, record_tokens

app = Flask(__name__)
CORS(app)

AGY_BINARY = agy_runner.AGY_BINARY
CONFIG_FILE = os.getenv("CONFIG_FILE", os.path.expanduser("~/agy_config.json"))
DEBUG_LOG = os.getenv("DEBUG", "false").lower() == "true"
DEBUG_LOG_DIR = os.getenv("DEBUG_LOG_DIR", agy_runner.AGY_CWD)

EFFORTS = ("low", "medium", "high")
EFFORT_OPTIONS = EFFORTS + ("auto",)
DEFAULT_EFFORT = "low"
BASE_MODEL = "gemini-3.8-flash"

# Model ids as `agy models` lists them (agy 1.4.10). Effort for Gemini is part
# of the id; the other models take no effort flag at all.
GEMINI_MODELS = tuple(f"{BASE_MODEL}-{e}" for e in EFFORTS)
OTHER_MODELS = ("claude-sonnet-4-6", "claude-opus-4-6-thinking", "gpt-oss-120b-medium")
CLI_MODELS = GEMINI_MODELS + OTHER_MODELS
# Prefix aliases so clients that send their own names (Claude Code, harnesses) land on a CLI model.
MODEL_ALIASES = (
    ("claude-opus", "claude-opus-4-6-thinking"),
    ("claude-sonnet", "claude-sonnet-4-6"),
    ("claude-haiku", f"{BASE_MODEL}-low"),      # Claude Code uses Haiku for quick background calls: keep it fast
    ("claude-3-5-haiku", f"{BASE_MODEL}-low"),
    ("claude", "claude-sonnet-4-6"),
    ("gpt-oss", "gpt-oss-120b-medium"),
)

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def _make_profile(pid, name, email, home, concurrency=4) -> dict:
    return {
        "id": pid,
        "name": name,
        "email": email,
        "home": os.path.expanduser(home),
        "semaphore": threading.Semaphore(int(concurrency)),
        "stats": {"requests": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0, "last_used": None},
        "lock": threading.Lock(),
    }


def _load_profiles() -> list:
    candidates = [
        os.getenv("PROFILES_CONFIG", ""),
        os.path.expanduser("~/profiles.json"),
        "profiles.json",
    ]
    for p_path in candidates:
        if p_path and os.path.isfile(p_path):
            try:
                with open(p_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                loaded = [
                    _make_profile(p["id"], p.get("name", f"Profile {p['id'].upper()}"),
                                  p.get("email", ""), p.get("home", "~"), p.get("concurrency", 4))
                    for p in raw
                ]
                if loaded:
                    return loaded
            except Exception as e:
                print(f"[WARN] Failed to load profiles from {p_path}: {e}")

    home = os.path.expanduser("~")
    return [
        _make_profile("p1", os.getenv("P1_NAME", "Profile 1"), os.getenv("P1_EMAIL", "profile1@example.com"),
                      os.getenv("P1_HOME", home)),
        _make_profile("p2", os.getenv("P2_NAME", "Profile 2"), os.getenv("P2_EMAIL", "profile2@example.com"),
                      os.getenv("P2_HOME", os.path.join(home, ".agy-perfil2"))),
        _make_profile("p3", os.getenv("P3_NAME", "Profile 3"), os.getenv("P3_EMAIL", "profile3@example.com"),
                      os.getenv("P3_HOME", os.path.join(home, ".agy-perfil3"))),
    ]


PROFILES = _load_profiles()
PROFILE_INDEX = {p["id"]: p for p in PROFILES}
POOL = WarmPool()


def _record_stats(profile, tokens_in: int, tokens_out: int, error: bool = False):
    with profile["lock"]:
        profile["stats"]["requests"] += 1
        profile["stats"]["tokens_in"] += tokens_in
        profile["stats"]["tokens_out"] += tokens_out
        if error:
            profile["stats"]["errors"] += 1
        profile["stats"]["last_used"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config (file-backed, cached by mtime so each request doesn't hit disk)
# ---------------------------------------------------------------------------

_config_lock = threading.Lock()
_config_cache = {"mtime": None, "data": None}


def _default_config() -> dict:
    return {
        "enabled_profiles": [PROFILES[0]["id"]],
        "active_profile": PROFILES[0]["id"],
        "active_effort": DEFAULT_EFFORT,
        "active_model": f"{BASE_MODEL}-{DEFAULT_EFFORT}",
        "agy_internal_tools": True,
    }


def _normalize_config(cfg: dict) -> dict:
    base = _default_config()
    if not isinstance(cfg, dict):
        return base
    out = {**base, **cfg}

    enabled = cfg.get("enabled_profiles")
    if not isinstance(enabled, list):  # legacy config: only active_profile present
        enabled = [out.get("active_profile")]
    enabled = [pid for pid in enabled if pid in PROFILE_INDEX]
    # keep declared order of PROFILES for determinism
    enabled = [p["id"] for p in PROFILES if p["id"] in enabled]
    if not enabled:
        enabled = [PROFILES[0]["id"]]
    out["enabled_profiles"] = enabled
    if out.get("active_profile") not in enabled:
        out["active_profile"] = enabled[0]

    eff = str(out.get("active_effort", DEFAULT_EFFORT)).lower()
    out["active_effort"] = eff if eff in EFFORT_OPTIONS else DEFAULT_EFFORT
    out["active_model"] = BASE_MODEL if out["active_effort"] == "auto" else f"{BASE_MODEL}-{out['active_effort']}"
    out["agy_internal_tools"] = bool(out.get("agy_internal_tools", True))
    return out


def get_config() -> dict:
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return _default_config()
    with _config_lock:
        if _config_cache["mtime"] == mtime and _config_cache["data"] is not None:
            return dict(_config_cache["data"])
        try:
            with open(CONFIG_FILE, "r") as f:
                data = _normalize_config(json.load(f))
        except Exception:
            data = _default_config()
        _config_cache.update(mtime=mtime, data=data)
        return dict(data)


def save_config(cfg: dict) -> dict:
    cfg = _normalize_config(cfg)
    with _config_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
        _config_cache.update(mtime=os.path.getmtime(CONFIG_FILE), data=cfg)
    return cfg


# ---------------------------------------------------------------------------
# Routing: direct (1 account) or round robin (2+), with fallback
# ---------------------------------------------------------------------------

_rr_lock = threading.Lock()
_rr_counter = random.randrange(1000)  # random start so Gunicorn workers don't all begin on the same account


def get_target_profiles(cfg: dict = None) -> list:
    """Ordered list: the account to try first, then the other enabled ones as fallback."""
    global _rr_counter
    cfg = cfg or get_config()
    enabled = [PROFILE_INDEX[pid] for pid in cfg["enabled_profiles"]]
    if len(enabled) <= 1:
        return enabled
    with _rr_lock:
        idx = _rr_counter % len(enabled)
        _rr_counter += 1
    return enabled[idx:] + enabled[:idx]


def routing_mode(cfg: dict = None) -> str:
    cfg = cfg or get_config()
    return "round_robin" if len(cfg["enabled_profiles"]) > 1 else "direct"


# ---------------------------------------------------------------------------
# Models / effort
# ---------------------------------------------------------------------------

def _model_entry(mid: str) -> dict:
    owner = "google" if mid.startswith("gemini") else "anthropic" if mid.startswith("claude") else "antigravity"
    return {"id": mid, "object": "model", "created": 1728864000, "owned_by": owner, "permission": [], "root": mid, "parent": None}


AVAILABLE_MODELS = [_model_entry(m) for m in GEMINI_MODELS + (BASE_MODEL,) + OTHER_MODELS]
MODEL_INDEX = {m["id"]: m for m in AVAILABLE_MODELS}


def effort_from_model(requested_model) -> str:
    """Effort encoded in the requested model name, or '' if none."""
    if not requested_model:
        return ""
    m = str(requested_model).strip().lower()
    for eff in EFFORTS:
        if m == eff or m.endswith(f"-{eff}") or m.endswith(f"/{eff}"):
            return eff
    if m.endswith("-med") or m == "med":
        return "medium"
    return ""


def resolve_effort(requested_model, cfg: dict) -> str:
    """Model suffix wins; otherwise the dashboard setting; 'auto' without a suffix means low."""
    eff = effort_from_model(requested_model)
    if eff:
        return eff
    active = cfg.get("active_effort", DEFAULT_EFFORT)
    return active if active in EFFORTS else DEFAULT_EFFORT


def resolve_model(requested_model, cfg: dict = None) -> str:
    """
    Map whatever the client asked for onto a CLI model id.
      * exact CLI id (any family)         -> itself
      * gemini-3.8-flash[-effort] / bare   -> effort from suffix, else dashboard effort
      * claude-* / gpt-oss-* aliases       -> nearest CLI model
      * anything else                     -> Gemini with the dashboard effort
    """
    cfg = cfg or get_config()
    m = str(requested_model or "").strip().lower()
    if m in CLI_MODELS:
        return m
    if not m.startswith("gemini"):
        for prefix, target in MODEL_ALIASES:
            if m.startswith(prefix):
                return target
    return f"{BASE_MODEL}-{resolve_effort(requested_model, cfg)}"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def _debug_log(name: str, text: str):
    if not DEBUG_LOG:
        return
    try:
        with open(os.path.join(DEBUG_LOG_DIR, name), "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def run_turn(prompt: str, model: str, profiles: list, internal_tools: bool = True):
    """
    Generator. Yields ("delta", text) while the model streams, then one final
    ("done", {"usage": {...}, "profile": profile, "response": full_text}).
    Tries each profile in order; raises AgyError if all fail before any text
    was streamed. If a profile fails after streaming started, yields
    ("error", message) and stops (we can't restart on another account
    without duplicating output).
    """
    tokens_in = _estimate_tokens(prompt)
    last_error = "No enabled account could process the request."

    for profile in profiles:
        streamed = ""
        proc = None
        with profile["semaphore"]:
            try:
                proc = POOL.acquire(profile, model, internal_tools)
                final = None
                for ev in proc.run_turn(prompt):
                    kind = ev.get("event")
                    if kind == "step_update":
                        su = ev.get("step_update", {})
                        if su.get("step_type", "agent_response") == "agent_response":
                            delta = su.get("text_delta")
                            if delta:
                                streamed += delta
                                yield ("delta", delta)
                    elif kind == "result":
                        final = ev.get("result", {})
                if final is None:
                    raise AgyError("agy ended without a result event")
                if final.get("status") != "SUCCESS":
                    raise AgyError(final.get("error") or f"agy status {final.get('status')}")

                response = final.get("response") or ""
                if not streamed and response:
                    streamed = response
                    yield ("delta", response)
                usage = final.get("usage") or {}
                in_tok = int(usage.get("input_tokens") or tokens_in)
                out_tok = int(usage.get("output_tokens") or _estimate_tokens(streamed))
                _record_stats(profile, in_tok, out_tok)
                record_tokens(profile["id"], model, in_tok, out_tok)
                _debug_log("agy_calls.log", f"--- {time.time()} {profile['id']}/{model} ok in={in_tok} out={out_tok}\n")
                yield ("done", {
                    "usage": {"prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok},
                    "profile": profile,
                    "response": streamed,
                })
                return
            except GeneratorExit:
                _record_stats(profile, tokens_in, _estimate_tokens(streamed))
                raise
            except AgyError as e:
                last_error = str(e)
                _record_stats(profile, tokens_in, 0, error=True)
                _debug_log("agy_calls.log", f"--- {time.time()} {profile['id']}/{model} ERROR {last_error}\n")
                if streamed:
                    yield ("error", last_error)
                    return
            except Exception as e:  # unexpected: treat like an upstream failure
                last_error = f"{type(e).__name__}: {e}"
                _record_stats(profile, tokens_in, 0, error=True)
                if streamed:
                    yield ("error", last_error)
                    return
            finally:
                if proc is not None:
                    proc.close()

    raise AgyError(last_error)


def make_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _openai_error(message: str, status: int = 502, etype: str = "server_error", code: str = "upstream_error"):
    return jsonify({"error": {"message": message, "type": etype, "param": None, "code": code}}), status


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------

@app.route("/v1/models", methods=["GET", "OPTIONS"])
@app.route("/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return ("", 204)
    return jsonify({"object": "list", "data": AVAILABLE_MODELS})


@app.route("/v1/models/<path:model_id>", methods=["GET", "OPTIONS"])
@app.route("/models/<path:model_id>", methods=["GET", "OPTIONS"])
def retrieve_model(model_id):
    if request.method == "OPTIONS":
        return ("", 204)
    model = MODEL_INDEX.get(model_id)
    if model is None:
        return jsonify({"error": {"message": f"Model '{model_id}' not found.", "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}), 404
    return jsonify(model)


# ---------------------------------------------------------------------------
# /v1/config
# ---------------------------------------------------------------------------

def _config_view(cfg: dict) -> dict:
    return {
        "enabled_profiles": cfg["enabled_profiles"],
        "routing_mode": routing_mode(cfg),
        "active_profile": cfg["active_profile"],
        "active_effort": cfg["active_effort"],
        "active_model": cfg["active_model"],
        "agy_internal_tools": cfg["agy_internal_tools"],
        "effort_options": list(EFFORT_OPTIONS),
        "profiles": [
            {"id": p["id"], "name": p["name"], "email": p["email"], "enabled": p["id"] in cfg["enabled_profiles"],
             "active": p["id"] in cfg["enabled_profiles"]}
            for p in PROFILES
        ],
        "models": [
            {"id": f"{BASE_MODEL}-{e}" if e != "auto" else BASE_MODEL, "name": f"Gemini 3.8 Flash ({e.capitalize()})",
             "effort": e, "active": cfg["active_effort"] == e}
            for e in EFFORT_OPTIONS
        ],
        "pool": POOL.stats(),
    }


@app.route("/v1/config", methods=["GET", "OPTIONS"])
def get_api_config():
    if request.method == "OPTIONS":
        return ("", 204)
    return jsonify(_config_view(get_config()))


@app.route("/v1/config", methods=["POST", "OPTIONS"])
def update_api_config():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True, silent=True) or {}
    cfg = get_config()
    errors = []

    if "enabled_profiles" in data:
        req = data["enabled_profiles"]
        if not isinstance(req, list) or not req:
            errors.append("enabled_profiles must be a non-empty list")
        else:
            unknown = [pid for pid in req if pid not in PROFILE_INDEX]
            if unknown:
                errors.append(f"unknown profiles: {', '.join(map(str, unknown))}")
            else:
                cfg["enabled_profiles"] = list(dict.fromkeys(req))

    if "active_profile" in data:  # backwards compatible: single-account mode
        pid = str(data["active_profile"]).strip()
        if pid in PROFILE_INDEX:
            cfg["enabled_profiles"] = [pid]
            cfg["active_profile"] = pid
        else:
            errors.append(f"unknown profile: {pid}")

    if "active_effort" in data:
        eff = str(data["active_effort"]).strip().lower()
        if eff in EFFORT_OPTIONS:
            cfg["active_effort"] = eff
        else:
            errors.append(f"active_effort must be one of {', '.join(EFFORT_OPTIONS)}")

    if "active_model" in data:
        eff = effort_from_model(data["active_model"])
        if eff:
            cfg["active_effort"] = eff
        elif str(data["active_model"]).strip().lower() == BASE_MODEL:
            cfg["active_effort"] = "auto"
        else:
            errors.append(f"unknown model: {data['active_model']}")

    if "agy_internal_tools" in data:
        cfg["agy_internal_tools"] = bool(data["agy_internal_tools"])

    if errors:
        return jsonify({"ok": False, "error": "; ".join(errors), "config": _config_view(get_config())}), 400

    cfg = save_config(cfg)
    POOL.prewarm([PROFILE_INDEX[pid] for pid in cfg["enabled_profiles"]], [resolve_model(None, cfg)], cfg["agy_internal_tools"])
    return jsonify({"ok": True, "config": _config_view(cfg)})


# ---------------------------------------------------------------------------
# Profiles / quota usage
# ---------------------------------------------------------------------------

_usage_cache: dict = {}
_usage_cache_ttl = 60
_usage_lock = threading.Lock()


def _get_token_info(profile):
    token_path = os.path.join(profile["home"], ".gemini", "antigravity-cli", "antigravity-oauth-token")
    try:
        with open(token_path, "r") as f:
            data = json.load(f)
        return {"token_valid": True, "expiry": data.get("token", {}).get("expiry", "")}
    except Exception as e:
        return {"token_valid": False, "expiry": None, "error": str(e)}


def _fetch_usage_raw(profile) -> dict:
    env = agy_runner.build_env(profile["home"])
    try:
        result = subprocess.run(
            list(agy_runner.AGY_COMMAND) + ["--print", "/usage", "--output-format", "json"],
            capture_output=True, text=True, timeout=30, env=env, cwd=agy_runner.AGY_CWD,
        )
        payload = json.loads(result.stdout.strip())
        cmd_data = payload.get("command", {}).get("data", {})
        return {"ok": True, "description": cmd_data.get("description", ""), "groups": cmd_data.get("groups", [])}
    except Exception as e:
        return {"ok": False, "error": str(e), "groups": []}


def get_usage(profile, force: bool = False) -> dict:
    pid = profile["id"]
    with _usage_lock:
        cached = _usage_cache.get(pid)
        if not force and cached and (time.time() - cached["fetched_at"]) < _usage_cache_ttl:
            return {**cached["data"], "cached": True, "fetched_at": cached["fetched_at"]}
    data = _fetch_usage_raw(profile)
    with _usage_lock:
        _usage_cache[pid] = {"data": data, "fetched_at": time.time()}
    return {**data, "cached": False, "fetched_at": time.time()}


@app.route("/v1/profiles", methods=["GET"])
def list_profiles():
    cfg = get_config()
    return jsonify({
        "routing_mode": routing_mode(cfg),
        "profiles": [
            {
                "id": p["id"], "name": p["name"], "email": p["email"], "home": p["home"],
                "enabled": p["id"] in cfg["enabled_profiles"],
                "active": p["id"] in cfg["enabled_profiles"],
                "token": _get_token_info(p),
                "stats": dict(p["stats"]),
            }
            for p in PROFILES
        ],
    })


@app.route("/v1/profiles/usage", methods=["GET"])
def all_profiles_usage():
    force = request.args.get("force", "false").lower() == "true"
    results: dict = {}

    def fetch(p):
        results[p["id"]] = {"id": p["id"], "name": p["name"], "email": p["email"], **get_usage(p, force=force)}

    threads = [threading.Thread(target=fetch, args=(p,)) for p in PROFILES]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=35)

    return jsonify({
        "profiles_usage": [results.get(p["id"], {"id": p["id"], "ok": False, "error": "timeout"}) for p in PROFILES],
        "timestamp": int(time.time()),
    })


@app.route("/v1/profiles/<profile_id>/usage", methods=["GET"])
def single_profile_usage(profile_id):
    p = PROFILE_INDEX.get(profile_id)
    if p is None:
        return jsonify({"error": f"Profile '{profile_id}' not found"}), 404
    force = request.args.get("force", "false").lower() == "true"
    return jsonify({"id": p["id"], "email": p["email"], **get_usage(p, force=force), "timestamp": int(time.time())})


@app.route("/v1/profiles/<profile_id>/health", methods=["GET"])
def profile_health(profile_id):
    p = PROFILE_INDEX.get(profile_id)
    if p is None:
        return jsonify({"error": f"Profile '{profile_id}' not found"}), 404
    return jsonify({"id": p["id"], "email": p["email"], "token": _get_token_info(p), "stats": dict(p["stats"]), "timestamp": int(time.time())})


@app.route("/v1/tokens/usage", methods=["GET", "OPTIONS"])
@app.route("/v1/token_usage", methods=["GET", "OPTIONS"])
def get_tokens_usage_endpoint():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        return jsonify(get_token_usage_summary())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------

def _parse_chat_request(data: dict):
    messages = data.get("messages") or []
    if not messages and "prompt" in data:
        messages = [{"role": "user", "content": data["prompt"]}]
    if not messages and "input" in data:  # /v1/responses style
        inp = data["input"]
        messages = inp if isinstance(inp, list) else [{"role": "user", "content": inp}]
    if not messages:
        return None, None

    tools = data.get("tools") or []
    if not tools and data.get("functions"):
        tools = [{"type": "function", "function": f} for f in data["functions"]]
    tool_choice = data.get("tool_choice")
    if tool_choice is None and data.get("function_call"):
        fc = data["function_call"]
        tool_choice = fc if isinstance(fc, str) else {"type": "function", "function": fc}
    if not tools:
        tool_choice = "none"
    return messages, {"tools": tools, "tool_choice": tool_choice}


@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
@app.route("/v1/responses", methods=["POST", "OPTIONS"])
def chat_completions():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True, silent=True) or {}
    _debug_log("agy_incoming.log", f"\n=== {time.time()} ===\n{json.dumps(data, indent=2, ensure_ascii=False)}\n")

    messages, tool_opts = _parse_chat_request(data)
    if not messages:
        return jsonify({"error": {"message": "Field 'messages' is required.", "type": "invalid_request_error", "param": "messages", "code": None}}), 400

    cfg = get_config()
    tools_enabled = tool_opts["tool_choice"] != "none"
    prompt = build_prompt(messages, tool_opts["tools"], tool_opts["tool_choice"])
    requested_model = data.get("model") or cfg["active_model"]
    model = resolve_model(requested_model, cfg)
    profiles = get_target_profiles(cfg)

    if data.get("stream", False):
        return _stream_chat(prompt, model, requested_model, profiles, cfg["agy_internal_tools"], tools_enabled)

    parser = ToolCallParser(tools_enabled)
    done = None
    try:
        for kind, payload in run_turn(prompt, model, profiles, cfg["agy_internal_tools"]):
            if kind == "delta":
                parser.feed(payload)
            elif kind == "done":
                done = payload
            elif kind == "error":
                return _openai_error(payload)
    except AgyError as e:
        return _openai_error(str(e))
    parser.finish()

    content = parser.text.strip()
    message = {"role": "assistant", "content": content or None}
    finish_reason = "stop"
    if parser.tool_calls:
        message["tool_calls"] = parser.tool_calls
        finish_reason = "tool_calls"
    elif not content:
        message["content"] = ""

    resp = jsonify({
        "id": make_id("chatcmpl"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason, "logprobs": None}],
        "usage": done["usage"],
        "system_fingerprint": None,
    })
    resp.headers["X-Profile-Used"] = done["profile"]["id"]
    resp.headers["X-Model-Used"] = model
    return resp


def _stream_chat(prompt, model, requested_model, profiles, internal_tools, tools_enabled) -> Response:
    cid = make_id("chatcmpl")
    ts = int(time.time())

    def chunk(delta: dict, finish_reason=None, extra: dict = None):
        body = {
            "id": cid, "object": "chat.completion.chunk", "created": ts, "model": requested_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}],
        }
        if extra:
            body.update(extra)
        return _sse(body)

    def generate():
        parser = ToolCallParser(tools_enabled)
        yield chunk({"role": "assistant", "content": ""})
        done = None
        try:
            for kind, payload in run_turn(prompt, model, profiles, internal_tools):
                if kind == "delta":
                    text = parser.feed(payload)
                    if text:
                        yield chunk({"content": text})
                elif kind == "done":
                    done = payload
                elif kind == "error":
                    yield _sse({"error": {"message": payload, "type": "server_error", "code": "upstream_error"}})
                    yield "data: [DONE]\n\n"
                    return
        except AgyError as e:
            yield _sse({"error": {"message": str(e), "type": "server_error", "code": "upstream_error"}})
            yield "data: [DONE]\n\n"
            return

        tail = parser.finish()
        if tail:
            yield chunk({"content": tail})

        finish = "stop"
        if parser.tool_calls:
            finish = "tool_calls"
            for i, call in enumerate(parser.tool_calls):
                yield chunk({"tool_calls": [{"index": i, "id": call["id"], "type": "function",
                                             "function": {"name": call["function"]["name"], "arguments": ""}}]})
                yield chunk({"tool_calls": [{"index": i, "function": {"arguments": call["function"]["arguments"]}}]})
        yield chunk({}, finish, {"usage": done["usage"] if done else None})
        yield "data: [DONE]\n\n"

    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", "X-Model-Used": model}
    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers=headers)


# ---------------------------------------------------------------------------
# POST /v1/completions (legacy)
# ---------------------------------------------------------------------------

@app.route("/v1/completions", methods=["POST", "OPTIONS"])
def completions():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True, silent=True) or {}
    prompt = data.get("prompt")
    if isinstance(prompt, list):
        prompt = " ".join(str(p) for p in prompt)
    if not prompt and data.get("messages"):
        prompt = build_prompt(data["messages"])
    if not prompt:
        return jsonify({"error": {"message": "Field 'prompt' is required.", "type": "invalid_request_error", "param": "prompt", "code": None}}), 400

    cfg = get_config()
    requested_model = data.get("model") or cfg["active_model"]
    model = resolve_model(requested_model, cfg)
    profiles = get_target_profiles(cfg)
    cid = make_id("cmpl")
    ts = int(time.time())

    if data.get("stream", False):
        def generate():
            try:
                for kind, payload in run_turn(prompt, model, profiles, cfg["agy_internal_tools"]):
                    if kind == "delta":
                        yield _sse({"id": cid, "object": "text_completion", "created": ts, "model": requested_model,
                                    "choices": [{"text": payload, "index": 0, "logprobs": None, "finish_reason": None}]})
                    elif kind == "error":
                        yield _sse({"error": {"message": payload, "type": "server_error", "code": "upstream_error"}})
                        yield "data: [DONE]\n\n"
                        return
            except AgyError as e:
                yield _sse({"error": {"message": str(e), "type": "server_error", "code": "upstream_error"}})
                yield "data: [DONE]\n\n"
                return
            yield _sse({"id": cid, "object": "text_completion", "created": ts, "model": requested_model,
                        "choices": [{"text": "", "index": 0, "logprobs": None, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"

        return Response(stream_with_context(generate()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    text, done = "", None
    try:
        for kind, payload in run_turn(prompt, model, profiles, cfg["agy_internal_tools"]):
            if kind == "delta":
                text += payload
            elif kind == "done":
                done = payload
            elif kind == "error":
                return _openai_error(payload)
    except AgyError as e:
        return _openai_error(str(e))

    resp = jsonify({
        "id": cid, "object": "text_completion", "created": ts, "model": requested_model,
        "choices": [{"text": text.strip(), "index": 0, "logprobs": None, "finish_reason": "stop"}],
        "usage": done["usage"],
    })
    resp.headers["X-Profile-Used"] = done["profile"]["id"]
    resp.headers["X-Model-Used"] = model
    return resp


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/v1/health", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    cfg = get_config()
    return jsonify({
        "status": "ok",
        "timestamp": int(time.time()),
        "agy_binary_found": agy_runner.resolve_binary_present(),
        "routing_mode": routing_mode(cfg),
        "enabled_profiles": cfg["enabled_profiles"],
        "active_profile": cfg["active_profile"],
        "active_effort": cfg["active_effort"],
        "active_model": cfg["active_model"],
        "models_available": [m["id"] for m in AVAILABLE_MODELS],
        "pool": POOL.stats(),
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": {"message": "Endpoint not found.", "type": "invalid_request_error", "param": None, "code": "endpoint_not_found"}}), 404


# ---------------------------------------------------------------------------
# Anthropic Messages API (Claude Code) on the same engine
# ---------------------------------------------------------------------------

import anthropic_api  # noqa: E402

anthropic_api.register(app, globals_module := __import__(__name__))


# ---------------------------------------------------------------------------
# Startup: pre-warm the pool for the current configuration
# ---------------------------------------------------------------------------

def _startup_prewarm():
    if os.getenv("AGY_PREWARM_ON_START", "true").lower() == "false":
        return
    cfg = get_config()
    POOL.prewarm([PROFILE_INDEX[pid] for pid in cfg["enabled_profiles"]], [resolve_model(None, cfg)], cfg["agy_internal_tools"])


_startup_prewarm()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    print(f"Starting Antigravity OpenAI-compatible API on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=DEBUG_LOG, threaded=True)
