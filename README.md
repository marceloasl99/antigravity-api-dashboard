<div align="center">

# ⚡ Antigravity API & Dashboard (v3.0)
### *OpenAI-Compatible Gateway for Gemini 3.8 Flash — Warm Process Pool, Tool Calling, Multi-Account Round Robin & Quota Monitoring*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](LICENSE)
[![Version: 3.0](https://img.shields.io/badge/Version-3.0.0-success.svg?style=for-the-badge)](https://github.com/marceloasl99/antigravity-api-dashboard/releases)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![OpenAI Compatible](https://img.shields.io/badge/API-OpenAI%20v1%20Compatible-412991.svg?style=for-the-badge&logo=openai&logoColor=white)](https://platform.openai.com/docs/api-reference)
[![Flask](https://img.shields.io/badge/Backend-Flask%20%26%20Gunicorn-000000.svg?style=for-the-badge&logo=flask&logoColor=white)](https://palletsprojects.com/p/flask/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=for-the-badge)](https://github.com/marceloasl99/antigravity-api-dashboard/pulls)

<p align="center">
  <a href="#-overview">Overview</a> •
  <a href="#-whats-new-in-v30">What's New in v3.0</a> •
  <a href="#-dashboard-preview">Dashboard Preview</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-quickstart">Quickstart</a> •
  <a href="#-multi-account-setup">Multi-Account Setup</a> •
  <a href="#-api-reference">API Reference</a> •
  <a href="#-integrations">Integrations</a> •
  <a href="#-version-history">Version History</a> •
  <a href="#-security">Security</a>
</p>

---

</div>

## 📖 Overview

**Antigravity API & Dashboard** converts Google's **Antigravity CLI (`agy`)** into a high-performance, self-hosted **OpenAI-compatible REST API gateway**. It empowers you to run **Gemini 3.8 Flash** with selectable reasoning effort levels, monitor real-time token consumption across multi-worker environments, switch active Google accounts with zero downtime, and inspect your Google account quotas through a modern web control panel.

Designed for self-hosters and power users integrating Gemini into tools like **LibreChat**, **OpenWebUI**, **LiteLLM**, **Cursor**, **Continue.dev**, and **LangChain**.

> 🔒 **100% Private & Self-Hosted**: Runs entirely in your private homelab or container (Proxmox LXC, Docker, Ubuntu/Debian). Built-in Privacy Mode protects and masks sensitive account credentials and emails.

---

## 🚀 What's New in v3.0

| Area | v2.0 | v3.0 |
| :--- | :--- | :--- |
| **Latency** | One cold `agy` process per request (~4–5 s of OAuth + `loadCodeAssist` before the first token) | **Warm pool** (`agy_runner.py`): processes are pre-started in `--input-format stream-json` mode and parked after their `init` event. A request takes a warm process, sends one turn and discards it. The cold start is paid in the background, not on the request path. |
| **Tool calling** | `tools` ignored, `tool_calls` never returned | **OpenAI function calling** (`tool_calling.py`): `tools`, `tool_choice`, `tool_calls` with `finish_reason: "tool_calls"`, streaming tool-call deltas, `role: "tool"` results round-tripped. Implemented as a prompt protocol (`<tool_call>` blocks) because the CLI has no way to receive tool results. |
| **Accounts** | Exactly one active account | **Enable 1, 2 or 3 accounts.** One enabled = direct routing. Two or more = round robin, with automatic fallback to the next enabled account when one fails. Disabled accounts are never used. |
| **Effort** | `low` / `medium` / `high` | `low` / `medium` / `high` / **`auto`**. A model suffix sent by the client (`gemini-3.8-flash-high`) always wins; `auto` without a suffix means `low`. |
| **Errors** | Failures returned as HTTP 200 with error text as the answer | Real **HTTP 502** in OpenAI error format (`{"error": {...}}`); in streams, an `error` event before `[DONE]`. |
| **Privacy** | Every request body logged to `/tmp/agy_incoming.log` | Request/call logs only when `DEBUG=true`. |
| **Engine** | Always `--dangerously-skip-permissions` | Dashboard toggle **"agy internal tools"**: whether the CLI may run its own tools (shell, files) on the server. Independent of client-side OpenAI tools. |
| **Claude Code** | — | Anthropic `/v1/messages` endpoint (streaming, tools, tool_result round trips) so `ANTHROPIC_BASE_URL` can point at the gateway. |
| **Models** | Gemini 3.8 Flash only | All ids from `agy models`: Gemini 3.8 Flash low/medium/high, Claude Sonnet 4.6, Claude Opus 4.6 (thinking), GPT-OSS 120B, plus aliases for client-side names. |
| **Tests** | None | `pytest` suite (58 tests) with a fake `agy` that speaks the documented stream-json protocol. Runs on Linux, macOS and Windows. |

```bash
pip install -r requirements-dev.txt && python -m pytest
```

### How the warm pool works

```
startup / config change ──► spawn agy (account, effort) ──► wait for {"event":"init"} ──► parked
request ──► take parked process ──► stdin {"event":"user",...} ──► stream step_update ──► result ──► close process
                                                                └── background refill of the pool slot
```

Pool keys are `(account, effort, internal_tools)` because `--model`/`--effort` are start-time flags. Tunables: `AGY_POOL_SIZE` (warm processes per key, default 1), `AGY_POOL_MAX_IDLE` (seconds before an idle process is recycled, default 600), `AGY_POOL_ENABLED=false` to disable. `/v1/health` and the dashboard show hits/misses.

### Measured on the real CLI (agy 1.4.10, Windows, 2026-10-30)

| Scenario | First token |
| :--- | :--- |
| v2.0 path: cold process, user's interactive HOME (8 MCP servers, 7 plugins, hooks) | **~37 s** (33 s of it is a fixed per-turn stall caused by that config, plus ~24k tokens of system prompt) |
| Clean dedicated HOME, cold process | ~5 s |
| Clean HOME + warm pool (v3) | **~1.8–2.5 s** (Gemini Flash), ~4 s (Claude Opus thinking), ~2k tokens of system prompt |

Three things make that possible, all automatic:

1. **Dedicated clean HOME per account** (`home` in `profiles.json`). Never point it at a HOME that carries your interactive agy config. On Windows the OAuth token lives in the Credential Manager, so a fresh HOME needs no new login; on Linux copy `~/.gemini/antigravity-cli/antigravity-oauth-token` or run `HOME=<dir> agy auth login` once.
2. **`--agent gateway`**: the gateway writes a custom agent (`<home>/.gemini/config/agents/gateway.md`) whose system prompt declares the CLI's built-in tools disabled and teaches the `<tool_call>` protocol. With the default coding agent the model used its own web search / file tools instead of the client's tools (1 of 4 prompts); with the gateway agent it complied 3/3 on Gemini Flash and Claude Sonnet.
3. **Effort in the model id, never `--effort`**: on 1.4.10 `--effort` conflicts with `gemini-3.8-flash-high` and is rejected by Claude/GPT-OSS models.

### Using it from Claude Code (Anthropic Messages API)

The gateway also serves `POST /v1/messages` (+ `/v1/messages/count_tokens`) in Anthropic format, so Claude Code can run on your Antigravity quota:

```bash
export ANTHROPIC_BASE_URL=http://<server-ip>:8080
export ANTHROPIC_API_KEY=anything                       # ignored, but Claude Code requires it
export ANTHROPIC_SMALL_FAST_MODEL=claude-3-5-haiku-20241022   # -> gemini-3.8-flash-low (fast background calls)
claude --model claude-sonnet-4-6
```

Model aliases: `claude-opus*` → `claude-opus-4-6-thinking`, `claude-sonnet*` → `claude-sonnet-4-6`, `claude-*haiku*` → `gemini-3.8-flash-low`, `gpt-oss*` → `gpt-oss-120b-medium`, anything else → Gemini 3.8 Flash with the dashboard effort. `GET /v1/models` lists the real CLI ids. Verified with `claude -p` on a sample project: 3 turns (two `Read` tool calls + answer) in 16 s. OpenAI-style clients (DeepSeek harness, LibreChat, Cursor…) use `/v1/chat/completions` as before.

Known limit: `/usage` (Google quota) is not answered by the CLI in print mode on 1.4.10, so the dashboard's quota cards show "no data" there.

### Tool calling example

```python
resp = client.chat.completions.create(
    model="gemini-3.8-flash-medium",
    messages=[{"role": "user", "content": "What's the weather in Lisbon?"}],
    tools=[{"type": "function", "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
)
call = resp.choices[0].message.tool_calls[0]        # finish_reason == "tool_calls"
# ... run the tool, then send {"role": "tool", "tool_call_id": call.id, "content": "22C sunny"} back as usual
```

---

## 📜 v2.0 (previous)

Version 2.0 represented a major architectural upgrade over Version 1.0:

| Feature | Version 1.0 (Legacy) | Version 2.0 (Current) |
| :--- | :--- | :--- |
| **Model Specialization** | Generic multi-model routing | **Gemini 3.8 Flash Exclusive** with dedicated reasoning effort control (`Low`, `Medium`, `High`) |
| **Account Routing** | Random round-robin rotation | **Direct Dedicated Routing**: Instant one-click selection of active account in UI without round-robin race conditions |
| **Token Tracking** | Transient in-memory request counter | **Persistent Multi-Worker Token Tracker (`token_tracker.py`)** with `fcntl` file-locking across Gunicorn workers |
| **Historical Analytics** | None | **Granular Breakdowns**: Hourly history (7-day rolling), Daily history, Monthly history, Usage by Account, and Recent Requests log |
| **Privacy Protection** | Plain text email display | **Built-in Privacy Mode (`🔒 Privacy Mode`)**: Enabled by default, masks sensitive Google account emails |
| **OpenAI Compatibility** | Basic completions | **100% Reliable SSE Streaming**: Guaranteed non-empty chunks and complete responses (eliminates `EMPTY_RESPONSE`) |
| **Concurrency Engine** | 7 sync workers | **Gunicorn `gthread`**: 3 workers x 8 threads (24 concurrent streaming connections) with keep-alive |
| **User Interface** | Portuguese dashboard | **100% English Modern Control Panel** with responsive auto-refresh intervals |

---

## 📸 Dashboard Preview

### 🎛️ v3.0: enabled accounts (round robin), effort incl. Auto, engine & warm pool, token usage, quota monitor
![AGY v3 Dashboard](dashboard_v3_full.png)

<details><summary>Previous version (v2.0)</summary>

![AGY Control Panel Overview v2](dashboard_v2_overview.png)
![AGY Dashboard Full View v2](dashboard_v2_full.png)

</details>

---

## 🏛️ Architecture

```mermaid
flowchart TD
    subgraph Clients ["💻 OpenAI-Compatible Clients"]
        C1["LibreChat / OpenWebUI"]
        C2["Cursor / Continue.dev"]
        C3["LangChain / Python / cURL"]
    end

    subgraph Gateway ["⚡ Antigravity API Gateway (Port 8080)"]
        G1["Gunicorn (3 Workers x 8 Threads)"]
        Config["Dynamic Config Store<br/>(agy_config.json)"]
        Router{"Direct Active Router"}
        Tracker["Persistent Token Tracker<br/>(token_tracker.py / fcntl)"]
        
        G1 --> Router
        Config -.-> Router
        Router --> Tracker
    end

    subgraph Accounts ["👤 Google Account Profiles"]
        P1["Profile 1 (Primary /root)"]
        P2["Profile 2 (/root/.agy-perfil2)"]
        P3["Profile 3 (/root/.agy-perfil3)"]
    end

    subgraph Execution ["⚙️ Antigravity CLI Execution Engine"]
        CLI["agy CLI Subprocess<br/>--model gemini-3.8-flash --effort low/med/high"]
        Safe["Non-Empty Stream Guard"]
    end

    subgraph Dashboard ["📊 Control Panel Web UI (Port 80)"]
        UI["Real-time Web App"]
        Privacy["Privacy Masking Engine"]
        Quotas["Real-time /usage Inspector"]
        UI <--> Privacy & Quotas
    end

    C1 & C2 & C3 -->|OpenAI REST / SSE Streaming| Gateway
    Router --> P1 & P2 & P3
    P1 & P2 & P3 --> CLI --> Safe
    Dashboard <-->|Admin Proxy| Gateway
```

---

## ⚡ Key Features

- **OpenAI v1 Compatibility**: Supports `/v1/chat/completions` (streaming & non-streaming), `/v1/responses`, `/v1/completions`, `/v1/models`, `/v1/config`, `/v1/tokens/usage`, `/v1/health`.
- **Gemini 3.8 Flash Reasoning Effort Selection**:
  - `🟢 Low` (`gemini-3.8-flash-low`): Near-instant response time, minimal latency for high-throughput agents.
  - `🟡 Medium` (`gemini-3.8-flash-medium`): Optimal balance between speed and structured analytical reasoning.
  - `🟣 High` (`gemini-3.8-flash-high`): Deep and reflective reasoning for complex code architectures and logic.
- **Dedicated Profile Selection**: Directly assign which Google account serves API traffic. Switch instantly from the web interface without restarting services.
- **Real-Time Token Usage Tracking**:
  - Automatically records prompt and completion tokens.
  - Tabbed metrics: **Hourly**, **Daily**, **Monthly**, **By Account**, and **Recent Requests**.
  - Safe across multi-process Gunicorn workers via POSIX file locking (`fcntl`).
- **Real-Time Google Quota Telemetry**: Reads live `/usage` statistics directly from the AGY CLI, displaying weekly and 5-hour limit percentages and reset countdowns.
- **Privacy Mode**: Toggle privacy with one click (`🔒 Privacy Mode: ON`) to mask sensitive email addresses (`ma••••0@gmail.com`) for public demos or screenshots.

---

## 🚀 Quickstart

### 1. Prerequisites
Ubuntu/Debian or Proxmox LXC container with Python 3.10+:

```bash
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip python3-flask python3-flask-cors gunicorn curl
```

### 2. Install the Antigravity CLI
Install the official Google Antigravity CLI:

```bash
curl -L https://antigravity.google/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
```

### 3. Clone Repository & Install Dependencies
```bash
git clone https://github.com/marceloasl99/antigravity-api-dashboard.git
cd antigravity-api-dashboard
pip install -r requirements.txt
```

---

## 🔐 Multi-Account Setup

Antigravity stores OAuth tokens inside `$HOME/.gemini/antigravity-cli/`. To run multiple isolated Google accounts simultaneously:

### Step 1: Authenticate Profile 1 (Default)
```bash
agy auth login
```

### Step 2: Authenticate Profile 2
```bash
mkdir -p /root/.agy-perfil2
HOME=/root/.agy-perfil2 /root/.local/bin/agy auth login
```

### Step 3: Authenticate Profile 3
```bash
mkdir -p /root/.agy-perfil3
HOME=/root/.agy-perfil3 /root/.local/bin/agy auth login
```

### Step 4: Configure Profiles
Create your local `profiles.json` (ignored by Git):
```bash
cp profiles.example.json profiles.json
```
```json
[
  {
    "id": "p1",
    "name": "Profile 1",
    "email": "your-account-1@gmail.com",
    "home": "/root",
    "concurrency": 4
  },
  {
    "id": "p2",
    "name": "Profile 2",
    "email": "your-account-2@gmail.com",
    "home": "/root/.agy-perfil2",
    "concurrency": 4
  },
  {
    "id": "p3",
    "name": "Profile 3",
    "email": "your-account-3@gmail.com",
    "home": "/root/.agy-perfil3",
    "concurrency": 4
  }
]
```

---

## 🔄 Systemd Service Setup

To keep the API and Dashboard running 24/7 as background daemons:

```bash
# 1. Copy service units
sudo cp systemd/agy-api.service /etc/systemd/system/
sudo cp systemd/agy-dashboard.service /etc/systemd/system/

# 2. Reload and enable services
sudo systemctl daemon-reload
sudo systemctl enable --now agy-api.service
sudo systemctl enable --now agy-dashboard.service

# 3. Verify status
sudo systemctl status agy-api.service
sudo systemctl status agy-dashboard.service
```

---

## 📡 API Reference

Base URL: `http://<server-ip>:8080/v1`

### Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `POST` | `/v1/chat/completions` | Standard OpenAI chat completion (streaming supported via `stream: true`) |
| `POST` | `/v1/responses` | OpenAI compatibility alias |
| `POST` | `/v1/completions` | Legacy completions endpoint |
| `GET` | `/v1/models` | List available Gemini 3.8 Flash model configurations |
| `GET` | `/v1/config` | Enabled accounts, routing mode, effort, engine options, pool stats |
| `POST` | `/v1/config` | `{"enabled_profiles": ["p1","p2"]}`, `{"active_effort": "auto"}`, `{"agy_internal_tools": false}` (legacy `{"active_profile": "p2"}` still works and means "only p2") |
| `GET` | `/v1/tokens/usage` | Retrieve accumulated token usage summary |
| `GET` | `/v1/profiles` | List configured accounts and real-time statistics |
| `GET` | `/v1/profiles/usage` | Fetch real-time Google account quota consumption |
| `GET` | `/v1/health` | Service health status |

### Dynamic Configuration via API

Switch the active account or reasoning effort programmatically:

```bash
# Round robin across Profile 1 and Profile 2
curl -X POST http://<server-ip>:8080/v1/config \
  -H "Content-Type: application/json" \
  -d '{"enabled_profiles": ["p1", "p2"]}'

# Only Profile 2 (direct routing)
curl -X POST http://<server-ip>:8080/v1/config -H "Content-Type: application/json" -d '{"active_profile": "p2"}'

# Reasoning effort: low | medium | high | auto
curl -X POST http://<server-ip>:8080/v1/config -H "Content-Type: application/json" -d '{"active_effort": "auto"}'
```

Every completion response carries `X-Profile-Used` and `X-Effort-Used` headers.

---

## 🔌 Integrations

### Python OpenAI SDK
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://10.0.0.108:8080/v1",  # Replace with your server IP
    api_key="none"
)

# Streaming completion with Gemini 3.8 Flash
stream = client.chat.completions.create(
    model="gemini-3.8-flash-low",
    messages=[
        {"role": "system", "content": "You are a concise engineering assistant."},
        {"role": "user", "content": "Summarize the key advantages of event-driven architecture."}
    ],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### cURL Streaming
```bash
curl -N -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash-medium",
    "stream": true,
    "messages": [
      {"role": "user", "content": "Write a Python script to sort a list of dictionaries by key."}
    ]
  }'
```

### LibreChat / OpenWebUI Configuration
```yaml
endpoints:
  custom:
    - name: "Antigravity Gateway (v2.0)"
      apiKey: "local"
      baseURL: "http://<YOUR_SERVER_IP>:8080/v1"
      models:
        default: ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"]
        fetch: true
```

---

## 📜 Version History

### Version 3.0.0 (Current)
- Warm process pool (`agy_runner.py`) — requests skip the CLI cold start.
- OpenAI tool calling (`tool_calling.py`), streaming included.
- 1–3 enabled accounts: direct routing or round robin with fallback.
- Effort `auto` (follows the client's model suffix).
- Upstream failures are HTTP 502 in OpenAI error format; debug logs opt-in.
- `agy internal tools` toggle; pytest suite with a simulated CLI.

### Version 2.0.0
- **Gemini 3.8 Flash Exclusive**: Fine-tuned execution parameters with selectable effort (`low`, `medium`, `high`).
- **Direct Account Routing**: Active account selector replacing unpredictable round-robin routing.
- **Persistent Token Tracker**: New `token_tracker.py` with multi-worker atomic locking (`fcntl`), hourly/daily/monthly/profile tables.
- **100% English Web Dashboard**: Full internationalization of the web control panel.
- **Privacy Mode**: Built-in privacy toggle to mask personal emails in the UI.
- **Production Gunicorn `gthread`**: Upgraded to 3 workers x 8 threads (24 concurrent streaming requests).
- **Non-Empty Stream Guarantee**: Eliminates empty chunk errors across all OpenAI client libraries.

### Version 1.0.0 (Legacy)
- Initial release featuring multi-account round-robin load balancing.
- Basic prefix routing (`p1/`, `p2/`, `p3/`).
- Initial Portuguese web dashboard monitoring Google quotas.
- Sync Gunicorn worker architecture.

---

## 🛡️ Security & Privacy

- **Strict Git Hygiene**: `.gitignore` ensures that `profiles.json`, `.env`, OAuth tokens (`.gemini/`, `.agy*`), and runtime logs are never committed.
- **Sensitive Data Masking**: All profile emails are masked by default in the UI to prevent accidental data leaks during screen sharing or screenshots.
- **Direct Connection**: No external servers, middleman APIs, or telemetry. All requests communicate directly between your server and Google AI endpoints.

---

## 📄 License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for details.

<div align="center">
  <sub>Built with ❤️ for homelabs, developers, and AI enthusiasts.</sub>
</div>
