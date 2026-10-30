"""
Subprocess runner + warm pool for the Antigravity CLI (agy).

Protocol (official headless docs):
  agy --input-format stream-json --output-format stream-json [...]
  stdin : {"event":"user","message":{"content":"..."}}\n   (one line per turn)
  stdout: NDJSON events: init -> step_update* -> result   (per turn)

The expensive part of an agy call (OAuth refresh, loadCodeAssist, model
listing) happens at process start. A process started with stream-json input
stays alive waiting on stdin, so we pre-spawn processes and wait for their
`init` event; a request then takes an already-warm process, sends exactly one
turn, reads until `result` and discards the process. One request per process
keeps conversations isolated (the CLI keeps conversation state per process).

Pool keys are (profile_id, model) because --model is fixed at process start
and slash commands are rejected in stream-json mode.
"""

import json
import os
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

AGY_BINARY = os.getenv("AGY_BINARY", os.path.expanduser("~/.local/bin/agy"))
# AGY_COMMAND lets tests (or exotic setups) run e.g. "python fake_agy.py".
AGY_COMMAND = shlex.split(os.getenv("AGY_COMMAND", "")) or [AGY_BINARY]
AGY_CWD = os.getenv("AGY_CWD", "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir())

POOL_SIZE = int(os.getenv("AGY_POOL_SIZE", "2"))              # warm processes per (profile, model); 1 leaves back-to-back requests cold
POOL_MAX_IDLE = float(os.getenv("AGY_POOL_MAX_IDLE", "600"))  # seconds before an idle warm process is recycled
POOL_INIT_TIMEOUT = float(os.getenv("AGY_INIT_TIMEOUT", "60"))
TURN_TIMEOUT = float(os.getenv("AGY_TURN_TIMEOUT", "180"))
POOL_ENABLED = os.getenv("AGY_POOL_ENABLED", "true").lower() != "false"

VALID_EFFORTS = ("low", "medium", "high")

# Custom agent installed into every profile HOME and selected with --agent.
# Measured on agy 1.4.10 with gemini-3.8-flash-low: with the default coding
# agent the model used its own tools (web search, file view) instead of the
# gateway's <tool_call> protocol (1-2 of 3 prompts); with this agent it
# complied 3/3. The agent body is the CLI-level system prompt.
AGY_AGENT = os.getenv("AGY_AGENT", "gateway")  # empty string = use the CLI's default agent
GATEWAY_AGENT_MD = """---
name: gateway
description: Plain chat model behind an OpenAI-compatible API gateway. No local tools.
tools: []
mcpServers: []
mainAgent: true
subagent: false
commandExecutionPolicy: off
---
You are a language model served through an API gateway to a remote client application. You are NOT running as a local coding agent.

ALL built-in tools of this environment (file viewing, editing, run_command, browser, search, MCP, subagents, etc.) are DISABLED for this session. Never call them and never say you will run or check something yourself.

The client may declare its own external tools inside the message, in a section titled "external tool protocol". Those are the ONLY tools that exist. To use one, finish your reply with one block per call, exactly:
<tool_call>{"name": "<tool name>", "arguments": {<json>}}</tool_call>
The client executes it and sends back a "Tool result" in the next message. If the user's request needs such a tool (reading files, running commands, fetching data), you MUST emit the <tool_call> block instead of answering from guesswork or explaining what you would do.

You do not know the client's working directory and the local workspace shown to you is NOT the user's project: when calling tools use paths relative to the user's project (or absolute paths the user gave), never paths from this environment.

If no tool is needed, answer the user directly and concisely, in the user's language. The message you receive may contain the conversation so far as a labelled transcript; reply only with your next message.
"""


class AgyError(RuntimeError):
    """Raised when a turn fails (non-SUCCESS result, crash, timeout)."""


def ensure_gateway_agent(home: str) -> str:
    """Write the gateway agent definition into <home>/.gemini/config/agents/ (idempotent)."""
    if not AGY_AGENT:
        return ""
    path = os.path.join(home, ".gemini", "config", "agents", f"{AGY_AGENT}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == GATEWAY_AGENT_MD:
                return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(GATEWAY_AGENT_MD)
    return path


def build_cmd(model: str, internal_tools: bool = True) -> list:
    """
    `model` is the full CLI model id (e.g. gemini-3.8-flash-high,
    claude-opus-4-6-thinking). Measured on agy 1.4.10: `--effort` conflicts
    with suffixed Gemini ids and is rejected by non-Gemini models, so the
    effort is always carried in the model id and --effort is never passed.
    """
    cmd = list(AGY_COMMAND) + [
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--model", model,
        "--disable-slash-commands",
    ]
    if AGY_AGENT:
        cmd += ["--agent", AGY_AGENT]
    if internal_tools:
        cmd.append("--dangerously-skip-permissions")
    return cmd


def build_env(home: str) -> dict:
    """
    Run the CLI with a dedicated HOME. Measured on agy 1.4.10: a HOME that
    carries the user's interactive config (MCP servers, plugins, hooks) adds a
    fixed ~33 s per turn and ~17k tokens of system prompt; a clean HOME answers
    in ~2 s. Windows resolves the home dir from USERPROFILE, so set both.
    """
    env = os.environ.copy()
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["PATH"] = f"{os.path.join(home, '.local', 'bin')}{os.pathsep}{env.get('PATH', '')}"
    return env


class AgyProcess:
    """One agy subprocess in stream-json mode. Single-use after warm-up."""

    def __init__(self, profile: dict, model: str, internal_tools: bool = True):
        self.profile = profile
        self.model = model
        self.key = (profile["id"], model, internal_tools)
        self.id = uuid.uuid4().hex[:8]
        self.created_at = time.time()
        self.ready = False
        self.init_event = None
        self._events: "queue.Queue" = queue.Queue()
        self._stderr = []
        ensure_gateway_agent(profile["home"])
        self.proc = subprocess.Popen(
            build_cmd(model, internal_tools),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=build_env(profile["home"]), cwd=AGY_CWD,
        )
        threading.Thread(target=self._read_stdout, daemon=True, name=f"agy-out-{self.id}").start()
        threading.Thread(target=self._read_stderr, daemon=True, name=f"agy-err-{self.id}").start()

    # -- readers -----------------------------------------------------------
    def _read_stdout(self):
        try:
            for raw in iter(self.proc.stdout.readline, ""):
                line = raw.strip()
                if not line:
                    continue
                try:
                    self._events.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self._events.put(None)  # EOF sentinel

    def _read_stderr(self):
        try:
            for raw in iter(self.proc.stderr.readline, ""):
                if len(self._stderr) < 50:
                    self._stderr.append(raw.rstrip())
        except Exception:
            pass

    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr).strip()

    def alive(self) -> bool:
        return self.proc.poll() is None

    # -- lifecycle ---------------------------------------------------------
    def wait_ready(self, timeout: float = POOL_INIT_TIMEOUT) -> bool:
        """Block until the `init` event arrives. Returns False on crash/timeout."""
        if self.ready:
            return True
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            try:
                ev = self._events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if not self.alive():
                    return False
                continue
            if ev is None:
                return False
            if ev.get("event") == "init":
                self.init_event = ev.get("init", {})
                self.ready = True
                return True
            # Anything else before init is unexpected; keep waiting.

    def run_turn(self, prompt: str, timeout: float = TURN_TIMEOUT):
        """
        Send one user message and yield parsed events until the `result`
        event (inclusive). Raises AgyError on failure.
        """
        payload = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        try:
            self.proc.stdin.write(payload)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise AgyError(f"agy stdin closed: {self.stderr_text or e}")

        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.close()
                raise AgyError(f"Request timed out after {int(timeout)}s")
            try:
                ev = self._events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not self.alive():
                    raise AgyError(f"agy exited (rc={self.proc.returncode}): {self.stderr_text or 'no stderr'}")
                continue
            if ev is None:
                raise AgyError(f"agy exited (rc={self.proc.returncode}): {self.stderr_text or 'no stderr'}")
            yield ev
            if ev.get("event") == "result":
                return

    def close(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        if self.alive():
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


class WarmPool:
    """Keeps POOL_SIZE ready AgyProcess instances per (profile, model, tools) key."""

    def __init__(self, size: int = POOL_SIZE, max_idle: float = POOL_MAX_IDLE, enabled: bool = POOL_ENABLED):
        self.size = max(0, size)
        self.max_idle = max_idle
        self.enabled = enabled and self.size > 0
        self._ready: dict = {}        # key -> list[AgyProcess]
        self._warming: dict = {}      # key -> int (spawns in flight)
        self._lock = threading.Lock()
        self._generation = 0  # bumped on shutdown so in-flight warm-ups are discarded
        self._stats = {"hits": 0, "misses": 0, "spawned": 0, "recycled": 0}

    # -- public ------------------------------------------------------------
    def acquire(self, profile: dict, model: str, internal_tools: bool = True) -> AgyProcess:
        key = (profile["id"], model, internal_tools)
        proc = None
        if self.enabled:
            with self._lock:
                bucket = self._ready.get(key, [])
                while bucket:
                    cand = bucket.pop(0)
                    if cand.alive() and (time.time() - cand.created_at) < self.max_idle:
                        proc = cand
                        break
                    cand.close()
                    self._stats["recycled"] += 1
                if proc is not None:
                    self._stats["hits"] += 1
                else:
                    self._stats["misses"] += 1
            self._refill_async(profile, model, internal_tools)
        if proc is None:
            proc = self._spawn(profile, model, internal_tools)
            if not proc.wait_ready():
                err = proc.stderr_text or "agy did not emit init"
                proc.close()
                raise AgyError(f"agy failed to start for profile {profile['id']}: {err}")
        return proc

    def prewarm(self, profiles: list, models: list, internal_tools: bool = True):
        for p in profiles:
            for m in models:
                self._refill_async(p, m, internal_tools)

    def stats(self) -> dict:
        with self._lock:
            ready = {f"{k[0]}/{k[1]}": len([p for p in v if p.alive()]) for k, v in self._ready.items()}
            return {
                "enabled": self.enabled,
                "size_per_key": self.size,
                "max_idle_seconds": self.max_idle,
                "ready": ready,
                "ready_total": sum(ready.values()),
                "warming": sum(self._warming.values()),
                **self._stats,
            }

    def shutdown(self, wait: float = 5.0):
        with self._lock:
            self._generation += 1
            procs = [p for b in self._ready.values() for p in b]
            self._ready.clear()
        for p in procs:
            p.close()
        deadline = time.time() + wait
        while time.time() < deadline:
            with self._lock:
                if not any(self._warming.values()):
                    return
            time.sleep(0.05)

    # -- internals ---------------------------------------------------------
    def _spawn(self, profile, model, internal_tools) -> AgyProcess:
        with self._lock:
            self._stats["spawned"] += 1
        return AgyProcess(profile, model, internal_tools)

    def _refill_async(self, profile, model, internal_tools):
        if not self.enabled:
            return
        key = (profile["id"], model, internal_tools)
        with self._lock:
            have = len(self._ready.get(key, [])) + self._warming.get(key, 0)
            missing = self.size - have
            if missing <= 0:
                return
            self._warming[key] = self._warming.get(key, 0) + missing
            gen = self._generation
        for _ in range(missing):
            threading.Thread(target=self._warm_one, args=(profile, model, internal_tools, key, gen),
                             daemon=True, name="agy-warm").start()

    def _warm_one(self, profile, model, internal_tools, key, gen):
        proc = None
        try:
            proc = self._spawn(profile, model, internal_tools)
            ok = proc.wait_ready()
        except Exception:
            ok = False
        with self._lock:
            self._warming[key] = max(0, self._warming.get(key, 0) - 1)
            if ok and proc is not None and gen == self._generation:
                self._ready.setdefault(key, []).append(proc)
                return
        if proc is not None:
            proc.close()


def resolve_binary_present() -> bool:
    """True if the configured agy command is executable (used by /v1/health)."""
    exe = AGY_COMMAND[0]
    return os.path.isfile(exe) or shutil.which(exe) is not None
