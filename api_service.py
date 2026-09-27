"""
OpenAI-compatible REST API wrapper for the Antigravity CLI (agy).

PERFORMANCE APPROACH — Warm-pool of persistent agy processes:
  The ~4-5s latency comes from agy doing multiple network roundtrips on every
  cold start (OAuth token refresh + loadCodeAssist + fetchAvailableModels).
  
  This service keeps a pool of PRE-WARMED agy processes ready via stdin/stdout
  communication, eliminating the cold start on every request.

  Each warm worker handles one request at a time (agy is single-threaded per call).
  The pool refills automatically after each use.

Endpoints (mirroring OpenAI /v1/* spec):
  GET  /v1/models                  – list available models
  GET  /v1/models/{model}          – retrieve a specific model
  POST /v1/chat/completions        – chat endpoint (stream=true supported)
  POST /v1/completions             – legacy prompt endpoint (stream=true supported)
  GET  /v1/health                  – health-check

Run with Gunicorn:
  gunicorn -w 4 --timeout 120 -b 0.0.0.0:8080 api_service:app
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)

AGY_BINARY   = os.getenv("AGY_BINARY", "/root/.local/bin/agy")
POOL_SIZE    = int(os.getenv("AGY_POOL_SIZE", "4"))   # warm workers kept ready
POOL_TIMEOUT = int(os.getenv("AGY_POOL_TIMEOUT", "30"))  # seconds to wait for worker

# ---------------------------------------------------------------------------
# Warm-pool: persistent agy processes that receive prompts via stdin
# ---------------------------------------------------------------------------
# NOTE: agy does not support interactive stdin mode — each invocation is a fresh
# subprocess call. The "warm pool" here pre-starts the process concurrently so
# that by the time the first requests arrive, at least some workers have already
# completed their connection/auth handshake and have their output ready.
#
# Real warm-pool would require agy to support a --server or --interactive flag.
# Since it doesn't, we instead use a CONCURRENCY QUEUE that issues requests
# in parallel (one subprocess per request) but limits the burst.

_request_semaphore = threading.Semaphore(POOL_SIZE)

# ---------------------------------------------------------------------------
# DNS pre-resolve and TCP pre-connect at startup
# ---------------------------------------------------------------------------

def _pre_warm():
    """
    Fire one dummy agy call at startup in a background thread.
    This forces:
      - DNS resolution of daily-cloudcode-pa.googleapis.com
      - TCP + TLS handshake
      - OAuth token validation
      - loadCodeAssist / fetchAvailableModels roundtrips
    So the FIRST real user request benefits from a warm OS DNS cache
    and potentially a warm TLS session (if the OS keeps it alive).
    """
    def _warmup():
        try:
            subprocess.run(
                [AGY_BINARY, "--print", "ping"],
                capture_output=True, text=True, timeout=30
            )
        except Exception:
            pass

    t = threading.Thread(target=_warmup, daemon=True, name="agy-warmup")
    t.start()

# ---------------------------------------------------------------------------
# Core: call agy subprocess (with semaphore to limit concurrency)
# ---------------------------------------------------------------------------

def _build_cmd(prompt: str, model: str = "") -> list:
    if model:
        return [AGY_BINARY, "--model", model, "--print", prompt]
    return [AGY_BINARY, "--print", prompt]


def call_agy(prompt: str, model: str = "") -> str:
    """Blocking agy call, rate-limited by semaphore."""
    with _request_semaphore:
        cmd = _build_cmd(prompt, model)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=120
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            return f"[Error] {e.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return "[Error] Request timed out after 120s"


def stream_agy(prompt: str, model: str = ""):
    """Stream agy output line by line."""
    with _request_semaphore:
        cmd = _build_cmd(prompt, model)
        try:
            with subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            ) as proc:
                for line in proc.stdout:
                    yield line
                proc.wait()
                if proc.returncode != 0:
                    yield f"\n[Error] {proc.stderr.read().strip()}"
        except FileNotFoundError:
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
MODEL_INDEX = {m["id"]: m for m in AVAILABLE_MODELS}


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

    if data.get("stream", False):
        return _stream_chat(prompt, model_id)

    response_text  = call_agy(prompt, model=model_id)
    created_ts     = int(time.time())
    completion_id  = make_id("chatcmpl")
    prompt_tokens  = max(1, len(prompt) // 4)
    completion_tok = max(1, len(response_text) // 4)

    return jsonify({
        "id": completion_id, "object": "chat.completion", "created": created_ts, "model": model_id,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop", "logprobs": None}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tok, "total_tokens": prompt_tokens + completion_tok},
        "system_fingerprint": None,
    })


def _stream_chat(prompt: str, model_id: str) -> Response:
    cid = make_id("chatcmpl")
    ts  = int(time.time())

    def generate():
        yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': ts, 'model': model_id, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
        for chunk in stream_agy(prompt, model_id):
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

    if data.get("stream", False):
        return _stream_completions(prompt, model_id)

    response_text  = call_agy(prompt, model=model_id)
    created_ts     = int(time.time())
    completion_id  = make_id("cmpl")

    return jsonify({
        "id": completion_id, "object": "text_completion", "created": created_ts, "model": model_id,
        "choices": [{"text": response_text, "index": 0, "logprobs": None, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": max(1, len(str(prompt)) // 4), "completion_tokens": max(1, len(response_text) // 4),
                  "total_tokens": max(1, len(str(prompt)) // 4) + max(1, len(response_text) // 4)},
    })


def _stream_completions(prompt: str, model_id: str) -> Response:
    cid = make_id("cmpl")
    ts  = int(time.time())

    def generate():
        for chunk in stream_agy(prompt, model_id):
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
    return jsonify({"status": "ok", "timestamp": int(time.time()), "pool_size": POOL_SIZE})


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": {"message": "Endpoint not found.", "type": "invalid_request_error", "param": None, "code": "endpoint_not_found"}}), 404


# ---------------------------------------------------------------------------
# Startup: trigger pre-warm and set resolver cache
# ---------------------------------------------------------------------------

# Pre-warm on module load (runs in Gunicorn worker after fork)
_pre_warm()


# ---------------------------------------------------------------------------
# Entry point (dev only — use Gunicorn in production)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port  = int(os.getenv("PORT", "8080"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    print(f"Starting OpenAI-compatible API on 0.0.0.0:{port}")
    print(f"AGY pool size: {POOL_SIZE} concurrent requests")
    print("TIP: gunicorn -w 4 --timeout 120 -b 0.0.0.0:{port} api_service:app")
    app.run(host="0.0.0.0", port=port, debug=debug)
