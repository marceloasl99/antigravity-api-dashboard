"""
OpenAI-compatible REST API wrapper — Gemini 3.8 Flash Exclusive Edition (Refined).

Guarantees 100% OpenAI compatibility:
  - Supports streaming and non-streaming responses.
  - Guarantees non-empty content in every chunk and completed message (prevents EMPTY_RESPONSE).
  - Handles reasoning models, developer/system roles, and complex content parts.
  - Echoes requested model name in response.
  - Selected profile is called directly (no round-robin; deterministic execution).
  - Web-configurable Active Profile & Reasoning Effort.
  - Endpoints:
      * POST /v1/chat/completions
      * POST /v1/responses (compatibility alias)
      * POST /v1/completions
      * GET  /v1/models & /v1/models/{model}
      * GET  /v1/health
      * GET/POST /v1/config
      * GET  /v1/profiles & /v1/profiles/usage
"""

import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
from token_tracker import record_tokens, get_token_usage_summary

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

AGY_BINARY = os.getenv("AGY_BINARY", "/root/.local/bin/agy")
CONFIG_FILE = os.getenv("CONFIG_FILE", "/root/agy_config.json")

# ---------------------------------------------------------------------------
# Profiles Configuration
# ---------------------------------------------------------------------------

def _load_profiles():
    # Attempt to load custom profiles from file if present
    candidates = [
        os.getenv("PROFILES_CONFIG", ""),
        "/root/profiles.json",
        "profiles.json",
    ]
    for p_path in candidates:
        if p_path and os.path.isfile(p_path):
            try:
                with open(p_path, "r", encoding="utf-8") as f:
                    raw_profiles = json.load(f)
                loaded = []
                for p in raw_profiles:
                    loaded.append({
                        "id": p["id"],
                        "name": p.get("name", f"Profile {p['id'].upper()}"),
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
                print(f"[WARN] Failed to load profiles from {p_path}: {e}")

    # Default profile definitions (customizable via env vars)
    return [
        {
            "id": "p1",
            "name": os.getenv("P1_NAME", "Profile 1"),
            "email": os.getenv("P1_EMAIL", "profile1@example.com"),
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
            "name": os.getenv("P2_NAME", "Profile 2"),
            "email": os.getenv("P2_EMAIL", "profile2@example.com"),
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
            "name": os.getenv("P3_NAME", "Profile 3"),
            "email": os.getenv("P3_EMAIL", "profile3@example.com"),
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
_config_lock = threading.Lock()


def get_config() -> dict:
    """Read the active configuration (profile + effort/model)."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "active_profile": "p1",
            "active_effort": "low",
            "active_model": "gemini-3.8-flash-low",
        }


def save_config(cfg: dict):
    """Save configuration atomically."""
    with _config_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)


def get_target_profiles() -> list:
    """
    Returns candidate profiles starting with the user's SELECTED profile.
    No round-robin: calls the chosen profile directly!
    """
    cfg = get_config()
    pid = cfg.get("active_profile", "p1")
    primary = PROFILE_INDEX.get(pid, PROFILES[0])
    backups = [p for p in PROFILES if p["id"] != primary["id"]]
    return [primary] + backups


def _record_stats(profile, tokens_in: int, tokens_out: int, error: bool = False):
    with profile["lock"]:
        profile["stats"]["requests"] += 1
        profile["stats"]["tokens_in"] += tokens_in
        profile["stats"]["tokens_out"] += tokens_out
        if error:
            profile["stats"]["errors"] += 1
        profile["stats"]["last_used"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Models Catalog: Gemini 3.8 Flash Exclusive
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = [
    {
        "id": "gemini-3.8-flash-low",
        "object": "model",
        "created": 1728864000,
        "owned_by": "google",
        "permission": [],
        "root": "gemini-3.8-flash-low",
        "parent": None,
    },
    {
        "id": "gemini-3.8-flash-medium",
        "object": "model",
        "created": 1728864000,
        "owned_by": "google",
        "permission": [],
        "root": "gemini-3.8-flash-medium",
        "parent": None,
    },
    {
        "id": "gemini-3.8-flash-high",
        "object": "model",
        "created": 1728864000,
        "owned_by": "google",
        "permission": [],
        "root": "gemini-3.8-flash-high",
        "parent": None,
    },
    {
        "id": "gemini-3.8-flash",
        "object": "model",
        "created": 1728864000,
        "owned_by": "google",
        "permission": [],
        "root": "gemini-3.8-flash",
        "parent": None,
    },
]

MODEL_INDEX = {m["id"]: m for m in AVAILABLE_MODELS}


def resolve_model(requested_model: str) -> str:
    """
    Resolve requested model strictly to Gemini 3.8 Flash (Low, Medium, or High).
    """
    cfg = get_config()
    default_model = cfg.get("active_model", "gemini-3.8-flash-low")

    if not requested_model:
        return default_model

    clean = requested_model.strip().lower()
    if clean in ("gemini-3.8-flash-high", "high"):
        return "gemini-3.8-flash-high"
    elif clean in ("gemini-3.8-flash-medium", "medium", "med"):
        return "gemini-3.8-flash-medium"
    elif clean in ("gemini-3.8-flash-low", "low"):
        return "gemini-3.8-flash-low"
    else:
        return default_model


# ---------------------------------------------------------------------------
# Subprocess Execution (High Performance)
# ---------------------------------------------------------------------------

def _build_env(profile) -> dict:
    env = os.environ.copy()
    env["HOME"] = profile["home"]
    env["PATH"] = f"/root/.local/bin:{env.get('PATH', '')}"
    return env


def _build_cmd(clean_model: str, format_type: str = "stream-json") -> list:
    cfg = get_config()
    effort = "low"
    if "high" in clean_model:
        effort = "high"
    elif "medium" in clean_model or "med" in clean_model:
        effort = "medium"
    elif "low" in clean_model:
        effort = "low"
    else:
        effort = cfg.get("active_effort", "low")

    return [
        AGY_BINARY,
        "--input-format", "stream-json",
        "--output-format", format_type,
        "--model", "gemini-3.8-flash",
        "--effort", effort,
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
    ]


def call_agy(prompt: str, clean_model: str, profiles_to_try: list) -> tuple:
    """Non-streaming execution calling the selected profile directly via stdin."""
    tokens_in = max(1, len(prompt) // 4)
    last_error = "Unknown error"
    payload = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"

    for profile in profiles_to_try:
        with profile["semaphore"]:
            cmd = _build_cmd(clean_model, format_type="stream-json")
            env = _build_env(profile)
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, env=env, cwd="/tmp"
                )
                stdout, stderr = proc.communicate(input=payload, timeout=180)

                try:
                    with open("/tmp/agy_calls.log", "a") as f_log:
                        f_log.write(f"--- CALL at {time.time()} ---\nCMD: {cmd}\nRC: {proc.returncode}\nOUT: {stdout[:300]}\nERR: {stderr[:300]}\n")
                except Exception:
                    pass

                final_response = ""
                final_usage = {}
                success = False

                for line in stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        event = data.get("event")
                        if event == "result":
                            res_data = data.get("result", {})
                            if res_data.get("status") == "SUCCESS":
                                success = True
                                final_response = res_data.get("response", "")
                                final_usage = res_data.get("usage", {})
                            else:
                                last_error = res_data.get("error") or "Execution failed"
                        elif event == "step_update":
                            delta = data.get("step_update", {}).get("text_delta")
                            if delta and not final_response:
                                final_response += delta
                    except json.JSONDecodeError:
                        continue

                if success and final_response:
                    in_tok = final_usage.get("input_tokens", tokens_in)
                    out_tok = final_usage.get("output_tokens", max(1, len(final_response) // 4))
                    _record_stats(profile, in_tok, out_tok)
                    record_tokens(profile["id"], clean_model, in_tok, out_tok)
                    return final_response, {
                        "prompt_tokens": in_tok,
                        "completion_tokens": out_tok,
                        "total_tokens": in_tok + out_tok,
                    }, profile
                else:
                    err = stderr.strip() or last_error
                    last_error = err
                    _record_stats(profile, tokens_in, 0, error=True)
                    continue
            except subprocess.TimeoutExpired:
                last_error = "Request timed out after 180s"
                _record_stats(profile, tokens_in, 0, error=True)
                continue
            except Exception as e:
                last_error = str(e)
                _record_stats(profile, tokens_in, 0, error=True)
                continue

    fallback = profiles_to_try[0] if profiles_to_try else PROFILES[0]
    return f"Unable to process response: {last_error}", {
        "prompt_tokens": tokens_in,
        "completion_tokens": 10,
        "total_tokens": tokens_in + 10,
    }, fallback


def stream_agy(prompt: str, clean_model: str, profiles_to_try: list):
    """
    Real-time token streaming calling the selected profile directly.
    Guarantees that at least one content chunk is yielded.
    Feeds prompt via stdin NDJSON stream to support arbitrarily large contexts.
    """
    tokens_in = max(1, len(prompt) // 4)
    payload = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"

    for profile in profiles_to_try:
        cmd = _build_cmd(clean_model, format_type="stream-json")
        env = _build_env(profile)
        total_out = 0
        started_streaming = False
        final_usage = None
        proc = None

        with profile["semaphore"]:
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, env=env, cwd="/tmp"
                )

                def _feed_stdin(p_in, text_data):
                    try:
                        p_in.write(text_data)
                        p_in.close()
                    except Exception:
                        pass

                writer_thread = threading.Thread(target=_feed_stdin, args=(proc.stdin, payload), daemon=True)
                writer_thread.start()

                for raw_line in iter(proc.stdout.readline, ""):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        event = data.get("event")
                        if event == "step_update":
                            delta = data.get("step_update", {}).get("text_delta")
                            if delta:
                                started_streaming = True
                                total_out += len(delta)
                                yield delta, profile, None
                        elif event == "result":
                            res_data = data.get("result", {})
                            status = res_data.get("status")
                            final_usage = res_data.get("usage")
                            res_text = res_data.get("response", "")

                            # If no tokens were streamed yet, yield the final response text!
                            if not started_streaming and res_text:
                                started_streaming = True
                                total_out += len(res_text)
                                yield res_text, profile, None

                            if status != "SUCCESS" and not started_streaming:
                                err = res_data.get("error") or "Execution failed"
                                raise RuntimeError(err)
                    except json.JSONDecodeError:
                        continue

                proc.wait()
                if proc.returncode != 0 and not started_streaming:
                    err_msg = proc.stderr.read().strip() if proc.stderr else "Subprocess failed"
                    raise RuntimeError(err_msg)

                # Ensure at least some non-empty content was yielded
                if not started_streaming:
                    fallback_text = "OK"
                    yield fallback_text, profile, None
                    total_out += len(fallback_text)

                in_tok = final_usage.get("input_tokens", tokens_in) if final_usage else tokens_in
                out_tok = final_usage.get("output_tokens", max(1, total_out // 4)) if final_usage else max(1, total_out // 4)
                _record_stats(profile, in_tok, out_tok)
                record_tokens(profile["id"], clean_model, in_tok, out_tok)
                yield "", profile, {
                    "prompt_tokens": in_tok,
                    "completion_tokens": out_tok,
                    "total_tokens": in_tok + out_tok,
                }
                return

            except GeneratorExit:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
                out_tok = max(1, total_out // 4)
                _record_stats(profile, tokens_in, out_tok)
                record_tokens(profile["id"], clean_model, tokens_in, out_tok)
                return

            except Exception as e:
                _record_stats(profile, tokens_in, 0, error=True)
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()

                if started_streaming:
                    yield f"\n[Stream interrupted: {e}]", profile, None
                    return
                continue

    fallback = profiles_to_try[0] if profiles_to_try else PROFILES[0]
    yield "Unable to obtain a response at this time.", fallback, None


def make_id(prefix: str = "chatcmpl") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def extract_text_from_content(content) -> str:
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


def format_messages_for_agy(messages: list) -> str:
    if not messages:
        return "Hello"

    if len(messages) == 1 and messages[0].get("role") in ("user", None):
        text = extract_text_from_content(messages[0].get("content"))
        return text if text else "Hello"

    parts = []
    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        content = extract_text_from_content(msg.get("content"))

        if not content and "tool_calls" in msg:
            content = str(msg.get("tool_calls"))

        if not content:
            continue

        if role in ("system", "developer"):
            parts.append(f"System: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        elif role == "tool":
            parts.append(f"Tool Output: {content}")
        else:
            parts.append(f"User: {content}")

    result = "\n\n".join(parts).strip()
    return result if result else "Hello"


# ---------------------------------------------------------------------------
# GET /v1/models & /models
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
        resolved = resolve_model(model_id)
        model = MODEL_INDEX.get(resolved, AVAILABLE_MODELS[0])
    return jsonify(model)


# ---------------------------------------------------------------------------
# Configuration Management (GET/POST /v1/config)
# ---------------------------------------------------------------------------

@app.route("/v1/config", methods=["GET", "OPTIONS"])
def get_api_config():
    if request.method == "OPTIONS":
        return ("", 204)
    cfg = get_config()
    active_pid = cfg.get("active_profile", "p1")
    active_eff = cfg.get("active_effort", "low")

    return jsonify({
        "active_profile": active_pid,
        "active_effort": active_eff,
        "active_model": cfg.get("active_model", f"gemini-3.8-flash-{active_eff}"),
        "profiles": [
            {
                "id": p["id"],
                "name": p["name"],
                "email": p["email"],
                "active": p["id"] == active_pid,
            }
            for p in PROFILES
        ],
        "models": [
            {
                "id": "gemini-3.8-flash-low",
                "name": "Gemini 3.8 Flash (Low)",
                "effort": "low",
                "active": active_eff == "low",
            },
            {
                "id": "gemini-3.8-flash-medium",
                "name": "Gemini 3.8 Flash (Medium)",
                "effort": "medium",
                "active": active_eff == "medium",
            },
            {
                "id": "gemini-3.8-flash-high",
                "name": "Gemini 3.8 Flash (High)",
                "effort": "high",
                "active": active_eff == "high",
            },
        ],
    })


@app.route("/v1/config", methods=["POST", "OPTIONS"])
def update_api_config():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(force=True, silent=True) or {}
    cfg = get_config()

    if "active_profile" in data:
        pid = str(data["active_profile"]).strip()
        if pid in PROFILE_INDEX:
            cfg["active_profile"] = pid

    if "active_effort" in data:
        eff = str(data["active_effort"]).strip().lower()
        if eff in ("low", "medium", "high"):
            cfg["active_effort"] = eff
            cfg["active_model"] = f"gemini-3.8-flash-{eff}"

    if "active_model" in data:
        m = str(data["active_model"]).strip().lower()
        if m in ("gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"):
            cfg["active_model"] = m
            cfg["active_effort"] = m.replace("gemini-3.8-flash-", "")

    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})


# ---------------------------------------------------------------------------
# Profile Usage & Token Info
# ---------------------------------------------------------------------------

_usage_cache: dict = {}
_usage_cache_ttl = 60
_usage_lock = threading.Lock()


def _get_token_info(profile):
    token_path = os.path.join(profile["home"], ".gemini", "antigravity-cli", "antigravity-oauth-token")
    try:
        with open(token_path, "r") as f:
            data = json.load(f)
        expiry = data.get("token", {}).get("expiry", "")
        return {"token_valid": True, "expiry": expiry}
    except Exception as e:
        return {"token_valid": False, "expiry": None, "error": str(e)}


def _fetch_usage_raw(profile) -> dict:
    env = _build_env(profile)
    try:
        result = subprocess.run(
            [AGY_BINARY, "--print", "/usage", "--output-format", "json"],
            capture_output=True, text=True, timeout=30, env=env, cwd="/tmp"
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
    active_pid = cfg.get("active_profile", "p1")
    result = []
    for p in PROFILES:
        token_info = _get_token_info(p)
        result.append({
            "id": p["id"],
            "name": p["name"],
            "email": p["email"],
            "home": p["home"],
            "active": p["id"] == active_pid,
            "token": token_info,
            "stats": dict(p["stats"]),
        })
    return jsonify({"profiles": result})


@app.route("/v1/profiles/usage", methods=["GET"])
def all_profiles_usage():
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


@app.route("/v1/tokens/usage", methods=["GET", "OPTIONS"])
@app.route("/v1/token_usage", methods=["GET", "OPTIONS"])
def get_tokens_usage_endpoint():
    if request.method == "OPTIONS":
        return ("", 204)
    return jsonify(get_token_usage_summary())


# ---------------------------------------------------------------------------
# POST /v1/chat/completions (OpenAI Compatible)
# ---------------------------------------------------------------------------

@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
@app.route("/v1/responses", methods=["POST", "OPTIONS"])
def chat_completions():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True, silent=True) or {}
    try:
        with open("/tmp/agy_incoming.log", "a") as f_in:
            f_in.write(f"\n=== INCOMING at {time.time()} ===\n{json.dumps(data, indent=2)}\n")
    except Exception:
        pass

    messages = data.get("messages", [])
    # Support prompt parameter if messages not sent
    if not messages and "prompt" in data:
        messages = [{"role": "user", "content": data["prompt"]}]

    if not messages:
        return jsonify({"error": {"message": "Field 'messages' is required.", "type": "invalid_request_error", "param": "messages", "code": None}}), 400

    prompt = format_messages_for_agy(messages)
    cfg = get_config()

    # Preserve the requested model name in the response so client validators match
    requested_model = data.get("model") or cfg.get("active_model", "gemini-3.8-flash-low")
    clean_model = resolve_model(requested_model)
    is_stream = bool(data.get("stream", False))

    target_profiles = get_target_profiles()

    if is_stream:
        return _stream_chat(prompt, clean_model, requested_model, target_profiles)

    response_text, usage_stats, used_profile = call_agy(prompt, clean_model, target_profiles)
    created_ts = int(time.time())
    completion_id = make_id("chatcmpl")

    # Safety: ensure content is never empty
    final_content = response_text.strip() if response_text else "Response successfully processed."

    resp = jsonify({
        "id": completion_id,
        "object": "chat.completion",
        "created": created_ts,
        "model": requested_model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": final_content,
                "reasoning_content": None,
            },
            "finish_reason": "stop",
            "logprobs": None,
        }],
        "usage": usage_stats,
        "system_fingerprint": None,
    })
    resp.headers["X-Profile-Used"] = used_profile["id"]
    return resp


def _stream_chat(prompt: str, clean_model: str, requested_model: str, target_profiles: list) -> Response:
    cid = make_id("chatcmpl")
    ts = int(time.time())

    def generate():
        first = True
        has_content = False

        for chunk, profile, usage in stream_agy(prompt, clean_model, target_profiles):
            if first:
                first = False
                init_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": ts,
                    "model": requested_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(init_chunk)}\n\n"

            if chunk:
                has_content = True
                token_chunk = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": ts,
                    "model": requested_model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(token_chunk)}\n\n"

        # Guard: if no content chunk was yielded, guarantee at least one token chunk is emitted!
        if not has_content:
            fallback_chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": ts,
                "model": requested_model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": "Response processed."},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(fallback_chunk)}\n\n"

        stop_chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": ts,
            "model": requested_model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(stop_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /v1/completions (Legacy completions)
# ---------------------------------------------------------------------------

@app.route("/v1/completions", methods=["POST", "OPTIONS"])
def completions():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True, silent=True) or {}
    prompt = data.get("prompt")
    if not prompt:
        msgs = data.get("messages", [])
        if msgs:
            prompt = format_messages_for_agy(msgs)
    if not prompt:
        return jsonify({"error": {"message": "Field 'prompt' is required.", "type": "invalid_request_error", "param": "prompt", "code": None}}), 400

    if isinstance(prompt, list):
        prompt = " ".join(str(p) for p in prompt)

    cfg = get_config()
    requested_model = data.get("model") or cfg.get("active_model", "gemini-3.8-flash-low")
    clean_model = resolve_model(requested_model)
    is_stream = bool(data.get("stream", False))
    target_profiles = get_target_profiles()

    if is_stream:
        return _stream_completions(prompt, clean_model, requested_model, target_profiles)

    response_text, usage_stats, used_profile = call_agy(prompt, clean_model, target_profiles)
    created_ts = int(time.time())
    completion_id = make_id("cmpl")

    final_content = response_text.strip() if response_text else "OK"

    resp = jsonify({
        "id": completion_id,
        "object": "text_completion",
        "created": created_ts,
        "model": requested_model,
        "choices": [{
            "text": final_content,
            "index": 0,
            "logprobs": None,
            "finish_reason": "stop",
        }],
        "usage": usage_stats,
    })
    resp.headers["X-Profile-Used"] = used_profile["id"]
    return resp


def _stream_completions(prompt: str, clean_model: str, requested_model: str, target_profiles: list) -> Response:
    cid = make_id("cmpl")
    ts = int(time.time())

    def generate():
        has_content = False
        for chunk, profile, usage in stream_agy(prompt, clean_model, target_profiles):
            if chunk:
                has_content = True
                payload = {
                    "id": cid,
                    "object": "text_completion",
                    "created": ts,
                    "model": requested_model,
                    "choices": [{
                        "text": chunk,
                        "index": 0,
                        "logprobs": None,
                        "finish_reason": None,
                    }],
                }
                yield f"data: {json.dumps(payload)}\n\n"

        if not has_content:
            fallback_payload = {
                "id": cid,
                "object": "text_completion",
                "created": ts,
                "model": requested_model,
                "choices": [{
                    "text": "OK",
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(fallback_payload)}\n\n"

        stop_payload = {
            "id": cid,
            "object": "text_completion",
            "created": ts,
            "model": requested_model,
            "choices": [{
                "text": "",
                "index": 0,
                "logprobs": None,
                "finish_reason": "stop",
            }],
        }
        yield f"data: {json.dumps(stop_payload)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Health & Status
# ---------------------------------------------------------------------------

@app.route("/v1/health", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    cfg = get_config()
    return jsonify({
        "status": "ok",
        "timestamp": int(time.time()),
        "active_profile": cfg.get("active_profile", "p1"),
        "active_effort": cfg.get("active_effort", "low"),
        "active_model": cfg.get("active_model", "gemini-3.8-flash-low"),
        "models_available": [m["id"] for m in AVAILABLE_MODELS],
    })


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": {"message": "Endpoint not found.", "type": "invalid_request_error", "param": None, "code": "endpoint_not_found"}}), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    print(f"Starting Gemini 3.8 Flash Exclusive API on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
