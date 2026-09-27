# Antigravity CLI — OpenAI-Compatible API & Multi-Account Dashboard

Turn Google's **Antigravity CLI (`agy`)** into a **100% OpenAI-compatible REST API** (`/v1/chat/completions`, `/v1/models`), featuring **Multi-Account Round-Robin load balancing** across multiple Google accounts and a **Real-Time Web Dashboard** for quota, token usage, latency monitoring, and dynamic profile management.

Engineered specifically for local homelabs and containerized environments (Proxmox LXC, Docker, Debian/Ubuntu VMs).

---

## 🚀 Key Features

- **OpenAI-Compatible API (`/v1`)**:
  - `POST /v1/chat/completions` (full support for Server-Sent Events (SSE) streaming and standard requests).
  - `GET /v1/models` and `GET /v1/models/{model_id}`.
  - `GET /v1/health`.
  - Drop-in compatibility with **LibreChat**, **OpenWebUI**, **LiteLLM**, **LangChain**, **Cursor**, **Continue**, etc.
- **Multi-Account / Multi-Profile Round-Robin**:
  - Distribute requests evenly across multiple Google accounts automatically to maximize rate limits and prevent quota throttling.
  - Smart prefix routing: force a specific account using a model prefix (e.g., `p1/gemini-3.8-flash-low`, `p2/claude-sonnet-4-6`) or omit prefix to round-robin among active accounts.
  - Isolated concurrency control via per-profile threading semaphores.
- **Modern Web Dashboard (Port 80)**:
  - Live metric counters for prompt/completion tokens, requests, and errors per account.
  - Real-time OAuth token expiry monitor.
  - Interactive profile toggles to enable/disable accounts from the round-robin pool on the fly with no restart required.
  - Built-in prompt playground for model testing directly from your browser.
- **Production-Ready for Homelabs**:
  - Pre-configured Gunicorn sync workers.
  - Native Systemd unit definitions with auto-restart on failure.
  - Built-in usage caching to avoid CLI overhead.

---

## 📁 Repository Structure

```text
├── api_service_multi.py     # Multi-profile OpenAI-compatible API (Gunicorn / Flask)
├── api_service.py           # Simplified single-profile API version
├── dashboard.py             # Real-time Web Dashboard (port 80)
├── profiles.example.json    # Account configuration template
├── .env.example             # Example environment variables
├── requirements.txt         # Python dependencies
├── systemd/                 # Production systemd service unit files
│   ├── agy-api.service
│   └── agy-dashboard.service
├── .gitignore               # Configured to prevent committing credentials/tokens
└── README.md                # Project documentation
```

---

## 🛠️ Prerequisites

1. **Linux OS** (Ubuntu 22.04 / 24.04, Debian 12, or Proxmox LXC container).
2. **Python 3.10+**.
3. **Antigravity CLI** installed and available in `$PATH` (typically `~/.local/bin/agy`):
   ```bash
   curl -L https://antigravity.google/install.sh | bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

---

## 🔐 Multi-Account Google Authentication

The Antigravity CLI stores authentication tokens inside `$HOME/.gemini/antigravity-cli/`. To isolate multiple accounts on the same machine, separate `$HOME` directories are used:

### 1. Authenticate Account 1 (Default Profile)
```bash
agy auth login
```

### 2. Authenticate Account 2 (Profile 2)
```bash
mkdir -p /root/.agy-perfil2
HOME=/root/.agy-perfil2 /root/.local/bin/agy auth login
```

### 3. Authenticate Account 3 (Profile 3)
```bash
mkdir -p /root/.agy-perfil3
HOME=/root/.agy-perfil3 /root/.local/bin/agy auth login
```

*(Optional)* Add quick aliases to your `~/.bashrc`:
```bash
alias agy2="HOME=/root/.agy-perfil2 /root/.local/bin/agy"
alias agy3="HOME=/root/.agy-perfil3 /root/.local/bin/agy"
```

---

## ⚙️ Installation & Setup

### 1. Install System & Python Dependencies
On Ubuntu/Debian:
```bash
apt-get update -y
apt-get install -y python3 python3-flask python3-flask-cors gunicorn
```
Or with `pip`:
```bash
pip install -r requirements.txt
```

### 2. Configure Profiles
Create your own `profiles.json` (this file is git-ignored for safety):
```bash
cp profiles.example.json profiles.json
```
Edit `profiles.json` with your desired account labels, emails, and home directories:
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
> **Note**: If `profiles.json` is omitted, the service falls back to environment variables (`P1_EMAIL`, etc.) or secure defaults.

---

## 🚦 Running Manually

To test the services prior to registering systemd daemons:

**Start the API:**
```bash
gunicorn --workers 7 --worker-class sync --timeout 120 --keep-alive 5 --bind 0.0.0.0:8080 api_service_multi:app
```

**Start the Dashboard (in a separate terminal):**
```bash
PORT=80 API_BASE=http://127.0.0.1:8080 python3 dashboard.py
```

---

## 🔄 Running as a Background Daemon (Systemd)

Copy the provided unit files to `/etc/systemd/system/`:

```bash
cp systemd/agy-api.service /etc/systemd/system/
cp systemd/agy-dashboard.service /etc/systemd/system/

systemctl daemon-reload

# Enable on boot
systemctl enable agy-api.service
systemctl enable agy-dashboard.service

# Start services
systemctl start agy-api.service
systemctl start agy-dashboard.service

# Verify status
systemctl status agy-api.service
systemctl status agy-dashboard.service
```

---

## 📡 API Usage Examples

### List Available Models
```bash
curl http://localhost:8080/v1/models
```

### Chat Completion (Auto Round-Robin across Active Accounts)
```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash-low",
    "messages": [
      {"role": "user", "content": "Explain quantum computing in one sentence."}
    ]
  }'
```

### Targeted Chat Completion (Specific Account)
Prepend the profile ID (`p1/`, `p2/`, or `p3/`) to the model identifier:
```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "p2/gemini-3.8-flash-low",
    "messages": [
      {"role": "user", "content": "Request routed explicitly to Profile 2."}
    ]
  }'
```

### Streaming Chat Completion
```bash
curl -N -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash-low",
    "stream": true,
    "messages": [
      {"role": "user", "content": "Write a short haiku about space."}
    ]
  }'
```

---

## 🔒 Security & Privacy

- **Zero Credentials in Git**: Local OAuth tokens (`antigravity-oauth-token`) and login states are stored exclusively in your local account directories and are excluded via `.gitignore`.
- **Pre-configured `.gitignore`**: Blocks `.env`, `profiles.json`, `.gemini`, `.agy*`, virtual environments, caches, and log files.
- **Local Network Scope**: Designed for local / homelab private networks. If exposing to the public internet, place behind a reverse proxy (Nginx, Traefik, Caddy, Cloudflare Tunnel) with SSL/TLS and Bearer token authentication.

---

## 📄 License
This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
