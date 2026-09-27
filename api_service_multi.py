"""
OpenAI-compatible REST API wrapper — Multi-Profile Edition.

Supports multiple Google accounts (profiles) via separate HOME directories:
  - Perfil 1: perfil1@exemplo.com     → HOME=/root
  - Perfil 2: perfil2@exemplo.com     → HOME=/root/.agy-perfil2
  - Perfil 3: perfil3@exemplo.com     → HOME=/root/.agy-perfil3

Endpoints:
  GET  /v1/models                  – list available models
  GET  /v1/models/{model}          – retrieve a specific model
  POST /v1/chat/completions        – chat endpoint (stream=true supported)
  POST /v1/completions             – legacy prompt endpoint
  GET  /v1/health                  – health-check
  GET  /v1/profiles                – list profiles and their status
  GET  /v1/profiles/{id}/health    – per-profile health

Model prefix routing:
  Model name prefix "p1/" → uses Perfil 1
  Model name prefix "p2/" → uses Perfil 2
  Model name prefix "p3/" → uses Perfil 3
  No prefix               → round-robins across all profiles

Run:
  gunicorn -w 7 --timeout 120 -b 0.0.0.0:8080 api_service_multi:app
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid
import base64
import re
from datetime import datetime, timezone
from collections import defaultdict

from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

AGY_BINARY = os.getenv("AGY_BINARY", "/root/.local/bin/agy")

# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------

def _load_profiles():
    # If a custom profiles.json file is present, load from it
    profiles_config_path = os.getenv("PROFILES_CONFIG", "profiles.json")
    if os.path.isfile(profiles_config_path):
        try:
            with open(profiles_config_path, "r", encoding="utf-8") as f:
                raw_profiles = json.load(f)
            loaded = []
            for p in raw_profiles:
                loaded.append({
                    "id": p["id"],
                    "name": p.get("name", p["id"]),
                    "email": p.get("email", ""),
                    "home": os.path.expanduser(p.get("home", "~")),
                    "semaphore": threading.Semaphore(int(p.get("concurrency", 4))),
                    "stats": {
                        "requests": 0,
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "errors": 0,
                        "last_used": None,
                    },
                    "lock": threading.Lock(),
                })
            if loaded:
                return loaded
        except Exception as e:
            print(f"[WARN] Failed to load {profiles_config_path}: {e}")

    # Default profile definitions (customizable via env vars)
    return [
        {
            "id": "p1",
            "name": os.getenv("P1_NAME", "Perfil 1"),
            "email": os.getenv("P1_EMAIL", "perfil1@exemplo.com"),
            "home": os.getenv("P1_HOME", "/root"),
            "semaphore": threading.Semaphore(4),
            "stats": {
                "requests": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "errors": 0,
                "last_used": None,
            },
            "lock": threading.Lock(),
        },
        {
            "id": "p2",
            "name": os.getenv("P2_NAME", "Perfil 2"),
            "email": os.getenv("P2_EMAIL", "perfil2@exemplo.com"),
            "home": os.getenv("P2_HOME", "/root/.agy-perfil2"),
            "semaphore": threading.Semaphore(4),
            "stats": {
                "requests": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "errors": 0,
                "last_used": None,
            },
            "lock": threading.Lock(),
        },
        {
            "id": "p3",
            "name": os.getenv("P3_NAME", "Perfil 3"),
            "email": os.getenv("P3_EMAIL", "perfil3@exemplo.com"),
            "home": os.getenv("P3_HOME", "/root/.agy-perfil3"),
            "semaphore": threading.Semaphore(4),
            "stats": {
                "requests": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "errors": 0,
                "last_used": None,
            },
            "lock": threading.Lock(),
        },
    ]

PROFILES = _load_profiles()
PROFILE_INDEX = {p["id"]: p for p in PROFILES}

# ---------------------------------------------------------------------------
# Profile enable/disable management
# Persisted to a JSON file so changes survive restarts and are visible across
# all Gunicorn workers (every worker re-reads on each routing decision).
# ---------------------------------------------------------------------------

_ENABLED_FILE = "/tmp/agy_enabled_profiles.json"
_enabled_write_lock = threading.Lock()


def _read_enabled_ids() -> set:
    """Read enabled profile IDs from persistent file. Defaults to all enabled."""
    try:
        with open(_ENABLED_FILE, "r") as f:
            data = json.load(f)
        ids = set(data.get("enabled", list(PROFILE_INDEX.keys())))
        return ids & set(PROFILE_INDEX.keys())
    except Exception:
        return set(PROFILE_INDEX.keys())  # all enabled by default


def _write_enabled_ids(enabled_ids: set):
    """Persist enabled profile IDs atomically."""
    tmp = _ENABLED_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"enabled": sorted(enabled_ids)}, f)
    os.replace(tmp, _ENABLED_FILE)


def get_enabled_profiles() -> list:
    """Return ordered list of enabled profile dicts."""
    enabled = _read_enabled_ids()
    return [p for p in PROFILES if p["id"] in enabled]


# ---------------------------------------------------------------------------
# Cross-process round-robin counter (shared across all Gunicorn workers)
# Uses a small binary file + fcntl exclusive lock so p1→p2→p3→p1 is
# guaranteed even with 7 concurrent workers.
# ---------------------------------------------------------------------------

import fcntl
import struct

_RR_FILE = "/tmp/agy_rr_counter.bin"


def _rr_next() -> int:
    """Atomically increment and return the cross-process round-robin counter."""
    with open(_RR_FILE, "a+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            data = f.read(4)
            val = struct.unpack("I", data)[0] if len(data) == 4 else 0
            next_val = val + 1
            f.seek(0)
            f.write(struct.pack("I", next_val))
            f.flush()
            return val          # return CURRENT value (pre-increment)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def get_profile_for_model(model_id: str):
    """Return (profile, clean_model_id) based on model prefix or round-robin over enabled profiles."""
    for p in PROFILES:
        prefix = p["id"] + "/"
        if model_id.startswith(prefix):
            return p, model_id[len(prefix):]
    # Strict cross-process round-robin over ENABLED profiles only
    active = get_enabled_profiles()
    if not active:
        # No profiles enabled — fall back to p1 to avoid crashing, caller may handle
        return PROFILES[0], model_id
    idx = _rr_next() % len(active)
    return active[idx], model_id



def _record_stats(profile, tokens_in: int, tokens_out: int, error: bool = False):
    with profile["lock"]:
        profile["stats"]["requests"] += 1
        profile["stats"]["tokens_in"] += tokens_in
        profile["stats"]["tokens_out"] += tokens_out
        if error:
            profile["stats"]["errors"] += 1
        profile["stats"]["last_used"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Core: call agy subprocess with per-profile HOME
# ---------------------------------------------------------------------------

def _build_env(profile) -> dict:
    env = os.environ.copy()
    env["HOME"] = profile["home"]
    return env


def _build_cmd(prompt: str, model: str = "") -> list:
    if model:
        return [AGY_BINARY, "--model", model, "--print", prompt]
    return [AGY_BINARY, "--print", prompt]


def call_agy(prompt: str, model: str = "", profile=None) -> str:
    """Blocking agy call using the given profile's HOME."""
    if profile is None:
        profile, model = get_profile_for_model(model)
    tokens_in = max(1, len(prompt) // 4)
    with profile["semaphore"]:
        cmd = _build_cmd(prompt, model)
        env = _build_env(profile)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                check=True, timeout=120, env=env
            )
            output = result.stdout.strip()
            _record_stats(profile, tokens_in, max(1, len(output) // 4))
            return output
        except subprocess.CalledProcessError as e:
            _record_stats(profile, tokens_in, 0, error=True)
            return f"[Error] {e.stderr.strip()}"
        except subprocess.TimeoutExpired:
            _record_stats(profile, tokens_in, 0, error=True)
            return "[Error] Request timed out after 120s"


def stream_agy(prompt: str, model: str = "", profile=None):
    """Stream agy output line by line using given profile's HOME."""
    if profile is None:
        profile, model = get_profile_for_model(model)
    tokens_in = max(1, len(prompt) // 4)
    with profile["semaphore"]:
        cmd = _build_cmd(prompt, model)
        env = _build_env(profile)
        total_out = 0
        try:
            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env
            ) as proc:
                for line in proc.stdout:
                    total_out += len(line)
                    yield line
                proc.wait()
                if proc.returncode != 0:
                    _record_stats(profile, tokens_in, max(1, total_out // 4), error=True)
                    yield f"\n[Error] {proc.stderr.read().strip()}"
                else:
                    _record_stats(profile, tokens_in, max(1, total_out // 4))
        except FileNotFoundError:
            _record_stats(profile, tokens_in, 0, error=True)
            yield f"[Error] AGY binary not found at {AGY_BINARY}"


def make_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = [
    {"id": "gemini-3.8-flash-high",    "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.8-flash-high",    "parent": None},
    {"id": "gemini-3.8-flash-medium",  "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.8-flash-medium",  "parent": None},
    {"id": "gemini-3.8-flash-low",     "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.8-flash-low",     "parent": None},
    {"id": "gemini-3.7-flash-high",    "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.7-flash-high",    "parent": None},
    {"id": "gemini-3.7-flash-medium",  "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.7-flash-medium",  "parent": None},
    {"id": "gemini-3.7-flash-low",     "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.7-flash-low",     "parent": None},
    {"id": "gemini-3.6-flash-high",    "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.6-flash-high",    "parent": None},
    {"id": "gemini-3.6-flash-medium",  "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.6-flash-medium",  "parent": None},
    {"id": "gemini-3.6-flash-low",     "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.6-flash-low",     "parent": None},
    {"id": "gemini-3.1-pro-high",      "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.1-pro-high",      "parent": None},
    {"id": "gemini-3.1-pro-low",       "object": "model", "created": 1700000000, "owned_by": "google",      "permission": [], "root": "gemini-3.1-pro-low",       "parent": None},
    {"id": "claude-sonnet-4-6",        "object": "model", "created": 1700000000, "owned_by": "anthropic",   "permission": [], "root": "claude-sonnet-4-6",        "parent": None},
    {"id": "claude-opus-4-6-thinking", "object": "model", "created": 1700000000, "owned_by": "anthropic",   "permission": [], "root": "claude-opus-4-6-thinking", "parent": None},
    {"id": "gpt-oss-120b-medium",      "object": "model", "created": 1700000000, "owned_by": "antigravity", "permission": [], "root": "gpt-oss-120b-medium",      "parent": None},
]
# Also add prefixed variants
_prefixed = []
for _p in PROFILES:
    for _m in AVAILABLE_MODELS:
        _prefixed.append({**_m, "id": f"{_p['id']}/{_m['id']}", "root": _m["id"]})
AVAILABLE_MODELS_ALL = AVAILABLE_MODELS + _prefixed
MODEL_INDEX = {m["id"]: m for m in AVAILABLE_MODELS_ALL}


@app.route("/v1/models", methods=["GET", "OPTIONS"])
@app.route("/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return ('', 204)
    return jsonify({"object": "list", "data": AVAILABLE_MODELS})


@app.route("/v1/models/<path:model_id>", methods=["GET", "OPTIONS"])
@app.route("/models/<path:model_id>", methods=["GET", "OPTIONS"])
def retrieve_model(model_id):
    if request.method == "OPTIONS":
        return ('', 204)
    model = MODEL_INDEX.get(model_id)
    if model is None:
        return jsonify({"error": {"message": f"Model '{model_id}' not found.", "type": "invalid_request_error", "param": "model", "code": "model_not_found"}}), 404
    return jsonify(model)


# ---------------------------------------------------------------------------
# GET /v1/profiles  +  /v1/profiles/usage
# ---------------------------------------------------------------------------

def _get_token_info(profile):
    """Read and parse the OAuth token file for a profile."""
    token_path = os.path.join(profile["home"], ".gemini", "antigravity-cli", "antigravity-oauth-token")
    try:
        with open(token_path, "r") as f:
            data = json.load(f)
        expiry = data.get("token", {}).get("expiry", "")
        return {"token_valid": True, "expiry": expiry}
    except Exception as e:
        return {"token_valid": False, "expiry": None, "error": str(e)}


# ---------------------------------------------------------------------------
# Usage: calls `agy --print /usage --output-format json` per profile
# Results are cached for 60 seconds to avoid hammering the CLI.
# ---------------------------------------------------------------------------

_usage_cache: dict = {}          # {profile_id: {"data": ..., "fetched_at": float}}
_usage_cache_ttl = 60            # seconds
_usage_lock = threading.Lock()


def _fetch_usage_raw(profile) -> dict:
    """Call agy --print /usage --output-format json and return parsed command.data."""
    env = _build_env(profile)
    try:
        result = subprocess.run(
            [AGY_BINARY, "--print", "/usage", "--output-format", "json"],
            capture_output=True, text=True, timeout=30, env=env
        )
        payload = json.loads(result.stdout.strip())
        cmd_data = payload.get("command", {}).get("data", {})
        return {
            "ok": True,
            "description": cmd_data.get("description", ""),
            "groups": cmd_data.get("groups", []),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "groups": []}


def get_usage(profile, force: bool = False) -> dict:
    """Return cached or freshly fetched usage for a profile."""
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
    result = []
    for p in PROFILES:
        token_info = _get_token_info(p)
        result.append({
            "id": p["id"],
            "name": p["name"],
            "email": p["email"],
            "home": p["home"],
            "token": token_info,
            "stats": dict(p["stats"]),
        })
    return jsonify({"profiles": result})


@app.route("/v1/profiles/usage", methods=["GET"])
def all_profiles_usage():
    """Fetch /usage for all 3 profiles concurrently."""
    force = request.args.get("force", "false").lower() == "true"
    results: dict = {}

    def fetch(p):
        results[p["id"]] = {
            "id": p["id"],
            "name": p["name"],
            "email": p["email"],
            **get_usage(p, force=force),
        }

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
    result = get_usage(p, force=force)
    return jsonify({"id": p["id"], "email": p["email"], **result, "timestamp": int(time.time())})


@app.route("/v1/profiles/<profile_id>/health", methods=["GET"])
def profile_health(profile_id):
    p = PROFILE_INDEX.get(profile_id)
    if p is None:
        return jsonify({"error": f"Profile '{profile_id}' not found"}), 404
    token_info = _get_token_info(p)
    return jsonify({
        "id": p["id"],
        "email": p["email"],
        "token": token_info,
        "stats": dict(p["stats"]),
        "timestamp": int(time.time()),
    })


# ---------------------------------------------------------------------------
# GET/POST /v1/profiles/enabled  — manage which profiles are active
# ---------------------------------------------------------------------------

@app.route("/v1/profiles/enabled", methods=["GET"])
def get_enabled():
    """Return which profiles are currently enabled for round-robin routing."""
    enabled_ids = _read_enabled_ids()
    result = []
    for p in PROFILES:
        result.append({
            "id": p["id"],
            "name": p["name"],
            "email": p["email"],
            "enabled": p["id"] in enabled_ids,
        })
    return jsonify({
        "profiles": result,
        "enabled_ids": sorted(enabled_ids),
        "total_enabled": len(enabled_ids),
    })


@app.route("/v1/profiles/enabled", methods=["POST"])
def set_enabled():
    """Set the list of enabled profiles. Body: {\"enabled\": [\"p1\",\"p3\"]}"""
    data = request.get_json(force=True, silent=True) or {}
    requested = set(data.get("enabled", []))
    # Validate IDs
    invalid = requested - set(PROFILE_INDEX.keys())
    if invalid:
        return jsonify({"error": f"Unknown profile IDs: {sorted(invalid)}"}), 400
    with _enabled_write_lock:
        _write_enabled_ids(requested)
    enabled_ids = _read_enabled_ids()
    return jsonify({
        "ok": True,
        "enabled_ids": sorted(enabled_ids),
        "total_enabled": len(enabled_ids),
    })


@app.route("/v1/profiles/<profile_id>/toggle", methods=["PATCH"])
def toggle_profile(profile_id):
    """Toggle a single profile on or off. Optionally body: {\"enabled\": true/false}"""
    p = PROFILE_INDEX.get(profile_id)
    if p is None:
        return jsonify({"error": f"Profile '{profile_id}' not found"}), 404
    with _enabled_write_lock:
        enabled_ids = _read_enabled_ids()
        data = request.get_json(force=True, silent=True) or {}
        if "enabled" in data:
            new_state = bool(data["enabled"])
        else:
            new_state = profile_id not in enabled_ids  # toggle
        if new_state:
            enabled_ids.add(profile_id)
        else:
            enabled_ids.discard(profile_id)
        _write_enabled_ids(enabled_ids)
    return jsonify({
        "ok": True,
        "id": profile_id,
        "enabled": new_state,
        "enabled_ids": sorted(enabled_ids),
    })


# ---------------------------------------------------------------------------
# POST /v1/chat/completions
# ---------------------------------------------------------------------------

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    data     = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": {"message": "Field 'messages' is required.", "type": "invalid_request_error", "param": "messages", "code": None}}), 400

    prompt_parts = []
    for msg in messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            prompt_parts.append(f"[System]: {content}")
        elif role == "assistant":
            prompt_parts.append(f"[Assistant]: {content}")
        else:
            prompt_parts.append(f"[User]: {content}")

    prompt   = "\n".join(prompt_parts)
    model_id = data.get("model", "gemini-3.8-flash-low")
    profile, clean_model = get_profile_for_model(model_id)

    if data.get("stream", False):
        return _stream_chat(prompt, clean_model, model_id, profile)

    response_text  = call_agy(prompt, model=clean_model, profile=profile)
    created_ts     = int(time.time())
    completion_id  = make_id("chatcmpl")
    prompt_tokens  = max(1, len(prompt) // 4)
    completion_tok = max(1, len(response_text) // 4)

    return jsonify({
        "id": completion_id, "object": "chat.completion", "created": created_ts, "model": model_id,
        "x_profile": profile["id"],
        "choices": [{"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop", "logprobs": None}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tok, "total_tokens": prompt_tokens + completion_tok},
        "system_fingerprint": None,
    })


def _stream_chat(prompt: str, clean_model: str, model_id: str, profile) -> Response:
    cid = make_id("chatcmpl")
    ts  = int(time.time())

    def generate():
        yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': ts, 'model': model_id, 'x_profile': profile['id'], 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
        for chunk in stream_agy(prompt, clean_model, profile):
            if chunk:
                yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': ts, 'model': model_id, 'choices': [{'index': 0, 'delta': {'content': chunk}, 'finish_reason': None}]})}\n\n"
        yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': ts, 'model': model_id, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# POST /v1/completions (legacy)
# ---------------------------------------------------------------------------

@app.route("/v1/completions", methods=["POST"])
def completions():
    data   = request.get_json(force=True, silent=True) or {}
    prompt = data.get("prompt")
    if not prompt:
        return jsonify({"error": {"message": "Field 'prompt' is required.", "type": "invalid_request_error", "param": "prompt", "code": None}}), 400
    if isinstance(prompt, list):
        prompt = " ".join(prompt)

    model_id = data.get("model", "gemini-3.8-flash-low")
    profile, clean_model = get_profile_for_model(model_id)

    if data.get("stream", False):
        return _stream_completions(prompt, clean_model, model_id, profile)

    response_text  = call_agy(prompt, model=clean_model, profile=profile)
    created_ts     = int(time.time())
    completion_id  = make_id("cmpl")

    return jsonify({
        "id": completion_id, "object": "text_completion", "created": created_ts, "model": model_id,
        "x_profile": profile["id"],
        "choices": [{"text": response_text, "index": 0, "logprobs": None, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": max(1, len(str(prompt)) // 4), "completion_tokens": max(1, len(response_text) // 4),
                  "total_tokens": max(1, len(str(prompt)) // 4) + max(1, len(response_text) // 4)},
    })


def _stream_completions(prompt: str, clean_model: str, model_id: str, profile) -> Response:
    cid = make_id("cmpl")
    ts  = int(time.time())

    def generate():
        for chunk in stream_agy(prompt, clean_model, profile):
            if chunk:
                yield f"data: {json.dumps({'id': cid, 'object': 'text_completion', 'created': ts, 'model': model_id, 'choices': [{'text': chunk, 'index': 0, 'logprobs': None, 'finish_reason': None}]})}\n\n"
        yield f"data: {json.dumps({'id': cid, 'object': 'text_completion', 'created': ts, 'model': model_id, 'choices': [{'text': '', 'index': 0, 'logprobs': None, 'finish_reason': 'stop'}]})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# GET /v1/health
# ---------------------------------------------------------------------------

@app.route("/v1/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "timestamp": int(time.time()),
        "profiles": len(PROFILES),
    })


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": {"message": "Endpoint not found.", "type": "invalid_request_error", "param": None, "code": "endpoint_not_found"}}), 404


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port  = int(os.getenv("PORT", "8080"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    print(f"Starting Multi-Profile OpenAI-compatible API on 0.0.0.0:{port}")
    print(f"Profiles: {[p['email'] for p in PROFILES]}")
    app.run(host="0.0.0.0", port=port, debug=debug)
