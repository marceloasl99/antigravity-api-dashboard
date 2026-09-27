# Antigravity CLI — OpenAI-Compatible API & Multi-Account Dashboard

Transforme o **Antigravity CLI (`agy`)** do Google em uma **API REST 100% compatível com a especificação OpenAI** (`/v1/chat/completions`, `/v1/models`), com suporte a **múltiplas contas Google em Round-Robin** e um **Dashboard Web em tempo real** para monitoramento de uso, cotas, latência e controle de perfis.

Projetado especialmente para uso local ou em containers (Proxmox LXC, Docker, VMs Debian/Ubuntu).

---

## 🚀 Funcionalidades

- **API Compatível com OpenAI (`/v1`)**:
  - `POST /v1/chat/completions` (suporte completo a streaming SSE e requisições normais).
  - `GET /v1/models` e `GET /v1/models/{model_id}`.
  - `GET /v1/health`.
  - Integrável diretamente com **LibreChat**, **OpenWebUI**, **LiteLLM**, **LangChain**, **Cursor**, etc.
- **Multi-Contas / Multi-Perfis com Round-Robin**:
  - Alterne e distribua as requisições entre várias contas Google automaticamente para multiplicar o limite de taxa (rate limits).
  - Roteamento inteligente: use o prefixo no modelo para forçar uma conta específica (ex: `p1/gemini-3.8-flash-low`, `p2/claude-sonnet-4-6`) ou omita o prefixo para usar round-robin entre contas ativas.
  - Controle de concorrência por semáforos isolados para cada perfil.
- **Dashboard Web Moderno (Porta 80)**:
  - Métricas de consumo de tokens (entrada/saída) e requisições por perfil.
  - Status de expiração do OAuth token de cada perfil.
  - Botões para ativar/desativar perfis do pool de round-robin dinamicamente sem reiniciar serviços.
  - Playground interativo para testar modelos e prompts diretamente do navegador.
- **Pronto para Produção Local**:
  - Gunicorn pré-configurado com múltiplos workers síncronos.
  - Unidades Systemd inclusas com auto-restart em caso de falha.
  - Cache de verificação de uso para evitar sobrecarga no CLI.

---

## 📁 Estrutura do Repositório

```text
├── api_service_multi.py     # API principal multi-perfil (Gunicorn / Flask)
├── api_service.py           # Versão simplificada de perfil único
├── dashboard.py             # Dashboard Web interativo (porta 80)
├── profiles.example.json    # Modelo para configuração das contas
├── .env.example             # Variáveis de ambiente de exemplo
├── requirements.txt         # Dependências Python
├── systemd/                 # Serviços systemd prontos para uso
│   ├── agy-api.service
│   └── agy-dashboard.service
├── .gitignore               # Configurado para bloquear credenciais e caches
└── README.md                # Este documento
```

---

## 🛠️ Pré-requisitos

1. **Linux** (Ubuntu 22.04 / 24.04, Debian 12 ou Proxmox LXC Container).
2. **Python 3.10+**.
3. **Antigravity CLI** instalado (geralmente em `~/.local/bin/agy`).
   ```bash
   curl -L https://antigravity.google/install.sh | bash
   export PATH="$HOME/.local/bin:$PATH"
   ```

---

## 🔐 Configurando Múltiplas Contas Google

O Antigravity CLI armazena autenticação em `$HOME/.gemini/antigravity-cli/`. Para isolar múltiplos perfis, usamos diretórios `$HOME` distintos:

### 1. Autenticar a Conta 1 (Perfil padrão)
```bash
agy auth login
```

### 2. Autenticar a Conta 2 (Perfil 2)
```bash
mkdir -p /root/.agy-perfil2
HOME=/root/.agy-perfil2 /root/.local/bin/agy auth login
```

### 3. Autenticar a Conta 3 (Perfil 3)
```bash
mkdir -p /root/.agy-perfil3
HOME=/root/.agy-perfil3 /root/.local/bin/agy auth login
```

*(Opcional)* Adicione aliases ao seu `~/.bashrc` para facilitar manutenções:
```bash
alias agy2="HOME=/root/.agy-perfil2 /root/.local/bin/agy"
alias agy3="HOME=/root/.agy-perfil3 /root/.local/bin/agy"
```

---

## ⚙️ Instalação e Configuração

### 1. Instalar Dependências do Sistema e Python
Em sistemas Ubuntu/Debian:
```bash
apt-get update -y
apt-get install -y python3 python3-flask python3-flask-cors gunicorn
```
Ou via `pip`:
```bash
pip install -r requirements.txt
```

### 2. Configurar Perfis
Você pode definir as contas criando um arquivo `profiles.json` (já ignorado pelo git):
```bash
cp profiles.example.json profiles.json
```
Edite `profiles.json` com os seus e-mails e diretórios correspondentes:
```json
[
  {
    "id": "p1",
    "name": "Perfil 1",
    "email": "sua-conta-1@gmail.com",
    "home": "/root",
    "concurrency": 4
  },
  {
    "id": "p2",
    "name": "Perfil 2",
    "email": "sua-conta-2@gmail.com",
    "home": "/root/.agy-perfil2",
    "concurrency": 4
  },
  {
    "id": "p3",
    "name": "Perfil 3",
    "email": "sua-conta-3@gmail.com",
    "home": "/root/.agy-perfil3",
    "concurrency": 4
  }
]
```
> **Nota**: Se `profiles.json` não existir, a API usará as variáveis de ambiente (`P1_EMAIL`, etc.) ou valores padrão seguros.

---

## 🚦 Executando Manualmente

Para testar antes de rodar como serviço:

**Iniciar a API:**
```bash
gunicorn --workers 7 --worker-class sync --timeout 120 --keep-alive 5 --bind 0.0.0.0:8080 api_service_multi:app
```

**Iniciar o Dashboard (em outro terminal):**
```bash
PORT=80 API_BASE=http://127.0.0.1:8080 python3 dashboard.py
```

---

## 🔄 Configurando como Serviço (Systemd)

Copie os arquivos da pasta `systemd/` para o diretório de serviços do Linux:

```bash
cp systemd/agy-api.service /etc/systemd/system/
cp systemd/agy-dashboard.service /etc/systemd/system/

systemctl daemon-reload

# Habilitar para inicializar com o sistema
systemctl enable agy-api.service
systemctl enable agy-dashboard.service

# Iniciar serviços
systemctl start agy-api.service
systemctl start agy-dashboard.service

# Verificar status
systemctl status agy-api.service
systemctl status agy-dashboard.service
```

---

## 📡 Exemplos de Uso da API

### Listar Modelos Disponíveis
```bash
curl http://localhost:8080/v1/models
```

### Chat Completion (com Round-Robin)
```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash-low",
    "messages": [
      {"role": "user", "content": "Olá! Explique o conceito de gravidade em uma frase."}
    ]
  }'
```

### Chat Completion Direcionado a um Perfil Específico
Basta colocar o ID do perfil antes do nome do modelo:
```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "p2/gemini-3.8-flash-low",
    "messages": [
      {"role": "user", "content": "Teste direcionado ao Perfil 2"}
    ]
  }'
```

---

## 🔒 Segurança e Privacidade

- **Zero Credenciais**: Nenhum token OAuth (`antigravity-oauth-token`) ou arquivo de autenticação pessoal deve ser enviado ao repositório.
- **Proteção do Git**: O arquivo `.gitignore` já está pré-configurado para ignorar `.env`, `profiles.json`, diretórios `.gemini`, caches e logs.
- **Uso Local**: Este serviço foi desenvolvido para rede local / homelab. Caso queira expor externamente, recomendamos utilizar um proxy reverso (Nginx, Caddy, Cloudflare Tunnel) com autenticação básica ou token Bearer.

---

## 📄 Licença
Distribuído sob a licença MIT. Consulte `LICENSE` para mais informações.
