#      EfeMultiAIbot — Self-Hosted AI Assistant Ecosystem

> **Admin Panel + Web Chat + WhatsApp Bot** powered by local LLMs.  
> 3-tier intelligent memory, real-time streaming, Google search integration, image analysis, and sandboxed code execution.
>
> **Author:** Efe Aydın · **License:** MIT

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Architecture](#-architecture)
- [Features](#-features)
- [Quick Start](#-quick-start)
- [Production Deployment](#-production-deployment)
- [Configuration](#-configuration)
- [API Reference](#-api-reference)
- [Memory System](#-3-tier-memory-system)
- [Project Structure](#-project-structure)
- [License](#-license)

---

## 🔭 Overview

LLaMA Panel is a **self-hosted AI assistant ecosystem** built by **Efe Aydın**, designed to run local LLMs (via [llama.cpp](https://github.com/ggerganov/llama.cpp), [Ollama](https://ollama.ai), or any OpenAI-compatible server) with three integrated interfaces:

| Component | File | Port | Description |
|-----------|------|------|-------------|
| 🖥️ **Admin Panel** | `app.py` | 5050 | Full LLM management dashboard with model control, memory management, bot monitoring, and admin chat |
| 💬 **Web Chat** | `chat_client.py` | 5051 | Standalone web chat UI with user accounts, rate limiting, file uploads, and AI expert tools |
| 📱 **WhatsApp Bot** | `whatsapp_bot.js` | — | Responds to WhatsApp messages with AI, Google search, image analysis, and conversation memory |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     NGINX (Reverse Proxy)                   │
│         SSE anti-buffering · SSL termination                │
├────────────────────────┬────────────────────────────────────┤
│   panel.localhost:80   │       chat.localhost:80            │
└────────────┬───────────┴──────────────────┬─────────────────┘
             │                              │
    ┌────────▼────────┐           ┌─────────▼─────────┐
    │   Admin Panel   │           │    Web Chat UI    │
    │   (Flask:5050)  │◄──────────│   (Flask:5051)    │
    │                 │  REST API │                   │
    │  • LLM Control  │           │  • User Sessions  │
    │  • Bot Monitor  │           │  • Rate Limiting  │
    │  • Memory Mgmt  │           │  • File Uploads   │
    │  • SSE Stream   │           │  • Expert Tools   │
    └────────┬────────┘           └───────────────────┘
             │
    ┌────────▼────────┐     ┌─────────────────────┐
    │  WhatsApp Bot   │     │   Local LLM Server  │
    │  (Node.js)      │     │ (llama.cpp / Ollama)│
    │                 │     │   :8080             │
    │  • QR Auth      │────►│   OpenAI-compat API │
    │  • Google Search│     └─────────────────────┘
    │  • Image Vision │
    └─────────────────┘
             │
    ┌────────▼────────┐
    │  SQLite (WAL)   │
    │                 │
    │  L1: LRU Cache  │ ← RAM (1024 keys, 5m TTL)
    │  L2: Compressed │ ← zlib-6, active messages
    │  L3: Archive    │ ← zlib-9, 30+ day old
    └─────────────────┘
```

---

## ✨ Features

### 🧠 3-Tier Memory System
- **L1 — LRU RAM Cache**: Thread-safe, TTL-based (5 min), 1024 keys, lazy eviction with periodic sweep
- **L2 — Compressed SQLite**: Active messages with zlib level-6 compression, buffered writes (batch INSERT)
- **L3 — Archive**: 30+ day old messages archived with zlib level-9 max compression
- **LLM Summarization**: Old context is summarized before archival — saves tokens without losing context
- **Token Budget**: Automatic message pruning to fit within model context window (default 32K tokens)

### 💬 Web Chat Interface
- **Premium Dark UI**: Glassmorphism design with custom serif/mono typography (Fraunces, DM Mono, DM Sans)
- **Real-time SSE Streaming**: Token-by-token response display with cursor animation
- **User Management**: Cookie-based sessions, customizable usernames
- **Rate Limiting**: Per-user hourly + daily message quotas (configurable per user)
- **File Uploads**: Images, documents, code files — drag-and-drop with preview strip
- **AI Expert Tools**:
  - 🧮 **Calculator**: Precise mathematical computations via double-prompt architecture
  - 💻 **Python Sandbox**: Execute Python code in isolated environment with screenshot capture
  - 🌐 **Web Search**: Real-time internet search via DuckDuckGo (no API key required)
  - 🧠 **Agentic Mode**: Multi-step reasoning with Analyze → Plan → Execute → Verify cycle
- **Date/Time Awareness**: Current date/time injected into system prompt for temporal context
- **Responsive Design**: Fully optimized for mobile, tablet, and desktop with touch support
- **Markdown Rendering**: Full markdown support with syntax-highlighted code blocks (highlight.js)

### 📱 WhatsApp Bot
- **Group & DM Support**: Respond in specified groups (via @mention) or direct messages (AI-enabled contacts)
- **Google Search**: Automatic internet search when real-time information is needed
- **Image Analysis**: Vision model support — describe, analyze, and discuss uploaded images
- **Conversation Memory**: Token-budgeted history fetched from the admin panel API
- **LaTeX Cleanup**: Automatic conversion of LaTeX math to WhatsApp-friendly plain text
- **Long Message Splitting**: Auto-chunking for responses exceeding WhatsApp's 4096 character limit
- **Agentic Mode**: Per-user switchable enhanced AI mode with custom system prompts
- **Bot Commands**: `!help`, `!status`, `!model`, `!clear`, `!ping` for bot management
- **Auto-Reconnect**: Exponential backoff reconnection on disconnects

### 🖥️ Admin Panel
- **Model Management**: Start/stop LLM server, configure parameters (temperature, top_p, top_k, etc.)
- **Multimodal Support**: mmproj projector toggle and selector UI for vision models
- **Chat Interface**: Built-in admin chat with system prompt customization
- **Contact Management**: View/toggle AI permissions per WhatsApp contact
- **Bot Control**: Start/stop/restart the WhatsApp bot process with health monitoring
- **Real-time Metrics**: SSE-powered live dashboard with message counts, cache stats, DB size
- **Maintenance**: Manual/automatic DB maintenance — VACUUM, WAL checkpoint, L2→L3 archival
- **Web Chat Users**: Manage web chat user accounts, limits, and expert tool access

### 🔒 Security
- **API Key Authentication**: Optional `PANEL_API_KEY` for all `/api/*` endpoints
- **Rate Limiting**: Configurable per-user hourly and daily message limits
- **Input Sanitization**: DOMPurify for HTML, secure filename handling for uploads
- **No Hardcoded Secrets**: All sensitive configuration via environment variables

---

## 🚀 Quick Start

### Prerequisites

- **Python** 3.10+
- **Node.js** 18+
- **Local LLM server** running on port 8080 (llama.cpp, Ollama, etc.)

### Installation

```bash
# Clone the repository
git clone https://github.com/eeea2222/EfeMultiAIbot.git
cd EfeMultiAIbot

# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Install Node.js dependencies (for WhatsApp bot)
npm install

# 3. Start the Admin Panel
python app.py

# 4. (Optional) Start the Web Chat in a separate terminal
python chat_client.py

# 5. Open the admin panel
# → http://localhost:5050

# 6. Open the web chat
# → http://localhost:5051
```

### CLI Options

```bash
python app.py                   # Panel (5050) + WhatsApp bot
python app.py --panel-only      # Web panel only (no bot)
python app.py --bot-only        # WhatsApp bot only
python app.py --setup           # Install npm dependencies
python app.py --stats           # Show memory/system statistics
python app.py --maintenance     # Run manual DB maintenance
python app.py --gunicorn        # Start with Gunicorn (production)
```

---

## 🏭 Production Deployment

### Gunicorn

```bash
# Method 1: Built-in flag
python app.py --gunicorn --port 5050
python chat_client.py --gunicorn --port 5051

# Method 2: Direct Gunicorn command
gunicorn app:app -c gunicorn.conf.py --bind 0.0.0.0:5050
gunicorn chat_client:app -c gunicorn.conf.py --bind 0.0.0.0:5051
```

> ⚠️ **Important**: Single worker mode is required — the application uses module-level singletons (MemoryManager, BotMonitor) that don't survive fork.

### Nginx

```bash
sudo cp nginx.conf /etc/nginx/sites-available/llama-panel
sudo ln -sf /etc/nginx/sites-available/llama-panel /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

> ⚠️ **Critical**: SSE endpoints (`/api/chat`, `/api/logs`, `/api/stats/stream`, `/api/webchat/chat`, `/api/send`) require `proxy_buffering off` — this is pre-configured in `nginx.conf`.

### Health Check

```bash
curl http://localhost:5050/health
# {"status":"healthy","db":"ok","llm":"stopped","bot":"stopped","uptime_s":42}
```

---

## ⚙️ Configuration

All configuration is via **environment variables** — no hardcoded secrets.

| Variable | Default | Description |
|----------|---------|-------------|
| `PANEL_API_KEY` | *(empty = no auth)* | API authentication key for `/api/*` endpoints |
| `MAIN_SERVER` | `http://127.0.0.1:5050` | Web Chat → Admin Panel address |
| `CHAT_PORT` | `5051` | Web Chat server port |
| `CHAT_HOST` | `0.0.0.0` | Web Chat bind host |
| `APP_TITLE` | `AI Chat` | Web Chat title |
| `PANEL_URL` | `http://127.0.0.1:5050` | WhatsApp Bot → Admin Panel address |
| `WHATSAPP_GROUPS` | `MyGroup1,MyGroup2` | Comma-separated group names the bot listens to |
| `SEARCH_COOLDOWN` | `15000` | Milliseconds between Google searches |
| `GUNICORN_THREADS` | `8` | Gunicorn thread count |
| `GUNICORN_BIND` | `0.0.0.0:5050` | Gunicorn bind address |

### Example `.env`

```bash
PANEL_API_KEY=your-secret-key-here
WHATSAPP_GROUPS=My Group,Another Group
MAIN_SERVER=http://192.168.1.5:5050
```

---

## 📡 API Reference

### LLM & Chat

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/server/status` | LLM server status (running, port, model) |
| `POST` | `/api/server/start` | Start LLM server |
| `POST` | `/api/server/stop` | Stop LLM server |
| `POST` | `/api/chat` | SSE streaming chat (admin panel) |
| `POST` | `/api/webchat/chat` | SSE streaming chat (web chat) |

### Messages & Memory

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/messages/<chat_id>` | Get message history (with optional `limit` and `budget`) |
| `POST` | `/api/messages/save` | Save a message |
| `DELETE` | `/api/messages/<chat_id>` | Delete chat history |

### Contacts & Users

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/contacts` | List all contacts |
| `POST` | `/api/contacts/upsert` | Create/update contact |
| `GET` | `/api/ai_enabled/<id>` | Check AI permission for contact |
| `POST` | `/api/ai_enabled/<id>/toggle` | Toggle AI permission |

### Web Chat Users

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/webchat/register` | Register/update web chat user |
| `GET` | `/api/webchat/limits/<uid>` | Get user rate limits |
| `POST` | `/api/send` | Send message via web chat (SSE stream) |

### Files & Uploads

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/upload` | Upload file (max 10MB) |
| `GET` | `/api/files` | List uploaded files |
| `GET` | `/api/files/<filename>` | Download a file |
| `DELETE` | `/api/files/<filename>` | Delete a file |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/api/stats` | Memory statistics |
| `GET` | `/api/stats/stream` | Live SSE metrics stream |
| `POST` | `/api/maintenance` | Trigger manual maintenance |

---

## 🗃️ 3-Tier Memory System

```
┌──────────────────────────────────────────────────────────┐
│  L1 — LRU RAM Cache                                      │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ • OrderedDict-based LRU, thread-safe (RLock)        │ │
│  │ • 1024 max keys, 300s TTL                           │ │
│  │ • Lazy eviction + periodic sweep (every 120s)       │ │
│  │ • Hit/miss/eviction metrics                         │ │
│  └─────────────────────────────────────────────────────┘ │
│                     ▼ cache miss                         │
│  L2 — SQLite Compressed (Active)                         │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ • WAL mode + mmap (512MB) + 64MB page cache         │ │
│  │ • zlib level-6 compression (skip if < 128 bytes)    │ │
│  │ • Write buffer: batch 50 rows or 2s timeout         │ │
│  │ • Max 250 messages per chat (auto-prune)            │ │
│  │ • Thread-local connection pool                      │ │
│  └─────────────────────────────────────────────────────┘ │
│                     ▼ 30+ days old                       │
│  L3 — SQLite Archive                                     │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ • zlib level-9 max compression                      │ │
│  │ • LLM summarization before archival                 │ │
│  │ • Still queryable via include_archive=True          │ │
│  │ • Periodic maintenance: VACUUM + WAL checkpoint     │ │
│  └─────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

### Image Storage
- **SHA-256 deduplication**: Same image uploaded twice → stored once, `ref_count` incremented
- **Adaptive compression**: zlib with automatic level selection
- **Thumbnail generation**: 64×64 JPEG thumbnails via Pillow (optional)
- **Automatic cleanup**: Images older than 60 days with `ref_count=0` are purged

---

## 📂 Project Structure

```
.
├── app.py                 # Admin Panel + Flask API + Memory Manager + Bot Monitor
├── chat_client.py         # Standalone Web Chat UI (self-contained HTML/CSS/JS)
├── whatsapp_bot.js        # WhatsApp bot (whatsapp-web.js)
├── gunicorn.conf.py       # Gunicorn production configuration
├── nginx.conf             # Nginx reverse proxy with SSE anti-buffering
├── requirements.txt       # Python dependencies
├── package.json           # Node.js dependencies
├── wait_screen.sh         # Screenshot capture helper for Sandbox
├── .gitignore             # Git exclusions
├── Sandbox/               # User sandbox files (auto-generated, gitignored)
├── uploads/               # Uploaded files (auto-generated, gitignored)
└── .wwebjs_auth/          # WhatsApp session data (auto-generated, gitignored)
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.10+, Flask, Gunicorn |
| Frontend | Vanilla HTML/CSS/JS (embedded in Python) |
| WhatsApp | whatsapp-web.js, Puppeteer |
| Database | SQLite (WAL mode, mmap, zlib compression) |
| Search | googlethis (optional) |
| Proxy | Nginx |
| LLM | Any OpenAI-compatible API (llama.cpp, Ollama, vLLM, etc.) |

---

## � Teşekkürler & Atıflar

Bu proje aşağıdaki açık kaynak proje ve kütüphaneler sayesinde mümkün olmuştur:

### Çekirdek Altyapı
| Proje | Lisans | Açıklama |
|-------|--------|----------|
| [Flask](https://github.com/pallets/flask) | BSD-3 | Python web framework |
| [Gunicorn](https://github.com/benoitc/gunicorn) | MIT | Python WSGI HTTP sunucusu |
| [SQLite](https://sqlite.org/) | Public Domain | Gömülü veritabanı motoru |
| [Nginx](https://github.com/nginx/nginx) | BSD-2 | Yüksek performanslı reverse proxy |

### WhatsApp & Bot
| Proje | Lisans | Açıklama |
|-------|--------|----------|
| [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js) | Apache-2.0 | WhatsApp Web API istemcisi |
| [Puppeteer](https://github.com/puppeteer/puppeteer) | Apache-2.0 | Headless Chrome/Chromium kontrolü |
| [qrcode-terminal](https://github.com/gtanner/qrcode-terminal) | Apache-2.0 | Terminal QR kodu oluşturucu |

### AI & LLM
| Proje | Lisans | Açıklama |
|-------|--------|----------|
| [llama.cpp](https://github.com/ggerganov/llama.cpp) | MIT | Yerel LLM çıkarım motoru |
| [Ollama](https://github.com/ollama/ollama) | MIT | Yerel LLM çalıştırma platformu |

### Frontend Kütüphaneleri
| Proje | Lisans | Açıklama |
|-------|--------|----------|
| [marked.js](https://github.com/markedjs/marked) | MIT | Markdown → HTML dönüştürücü |
| [highlight.js](https://github.com/highlightjs/highlight.js) | BSD-3 | Sözdizimi renklendirme |
| [DOMPurify](https://github.com/cure53/DOMPurify) | Apache-2.0/MPL-2.0 | XSS korumalı HTML sanitizasyonu |
| [Google Fonts](https://fonts.google.com/) | OFL/Apache-2.0 | Fraunces, DM Mono, DM Sans yazı tipleri |

### Yardımcı Kütüphaneler
| Proje | Lisans | Açıklama |
|-------|--------|----------|
| [Axios](https://github.com/axios/axios) | MIT | HTTP istemcisi (Node.js) |
| [Requests](https://github.com/psf/requests) | Apache-2.0 | HTTP istemcisi (Python) |
| [Pillow](https://github.com/python-pillow/Pillow) | HPND | Görsel işleme (thumbnail) |
| [googlethis](https://github.com/LuanRT/googlethis) | MIT | Google arama entegrasyonu |
| [Werkzeug](https://github.com/pallets/werkzeug) | BSD-3 | WSGI yardımcı kütüphanesi |

> Bu projelerin geliştiricilerine ve açık kaynak topluluğuna teşekkürler! ❤️

---
## ⚠️ Bildiri (Disclaimer)

> **Bu proje eğitim ve kişisel kullanım amaçlıdır.**

- Bu yazılım **"olduğu gibi"** (as-is) sunulmaktadır. Yazar, yazılımın kullanımından doğabilecek herhangi bir zarardan **sorumlu değildir**.
- WhatsApp botu, [whatsapp-web.js](https://github.com/pedroslopez/whatsapp-web.js) kütüphanesini kullanır. Bu kütüphane WhatsApp'ın **resmi olmayan** bir API'sidir. Kullanımı WhatsApp'ın [Hizmet Şartları](https://www.whatsapp.com/legal/terms-of-service)'na aykırı olabilir. **Hesap askıya alma riski kullanıcıya aittir.**
- Bu yazılımı **spam, taciz, izinsiz veri toplama** veya yasalara aykırı herhangi bir amaçla kullanmak **kesinlikle yasaktır**.
- LLM (Büyük Dil Modeli) çıktıları her zaman doğru olmayabilir. Yapay zeka yanıtlarını **doğrulamadan kritik kararlarda kullanmayın**.
- Kullanıcı verilerinin (sohbet geçmişi, yüklenen dosyalar) güvenliği tamamen **sunucu yöneticisinin sorumluluğundadır**.

---

## 📄 License

MIT License — Copyright © 2026 **Efe Aydın**

Herkes özgürce kullanabilir, değiştirebilir ve dağıtabilir.  
Tek koşul: **copyright notice (Efe Aydın ismi) korunmalıdır.**

Detaylar için [LICENSE](LICENSE) dosyasına bakın.
