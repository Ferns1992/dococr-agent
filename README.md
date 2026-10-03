# 📄 DocOCR Agent

> **Your documents, searchable.** Upload a PDF, a CV, a screenshot or a URL — then ask questions and get answers that cite exactly where the information came from.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 🤔 What does this actually do?

Drop in documents. Ask a question. Get an answer that shows its work.

**Without this kind of tool**, if your CV lists *Ollama* under skills and you want to know what that means, you're copying it into some chatbot. The chatbot answers from its own training data, and you have no idea whether it's right, current, or specific to *you*.

**With DocOCR Agent**, you get three deliberate answer modes, and you always know which one you're getting:

| Mode | What it does | When to use it |
|------|--------------|----------------|
| 📄 **Documents** | Answers *only* from your uploads, and cites them | Anything that must be traceable — contract terms, specs, your own records |
| 🧠 **+ Knowledge** | Your documents first, then general knowledge, clearly marked `(not in your documents)` | "My CV mentions Ollama — what is it?" |
| 🔬 **Research** | Your documents **plus a live web search**, with separate `[n]` and `[Wn]` citations | Current facts, pricing, anything that changes over time |

The point of the separation is that you never have to guess whether an answer came from your data or from the model's memory. Document claims are cited with `[1]`. Web claims are cited with `[W1]` and carry a link. Anything unverified is labelled rather than quietly blended in.

---

## ✨ Features

### 📥 Ingest anything
- **Documents** — PDF, TXT, Markdown, DOCX, HTML
- **Images** — PNG, JPG, with **OCR** so screenshots and scanned pages become searchable text
- **Links** — paste a URL and the page is fetched, cleaned of navigation chrome, and indexed
- **Visual search** — describe an image in words (*"architecture diagram with arrows"*) and find it

### 💬 Answers you can trust
- Every claim carries a citation to the passage it came from
- Click a source to see the exact excerpt, page number, and thumbnail
- **Research mode** separates your documents from the open web, so a citation always tells you which is which
- Streaming responses — you read as it writes

### 📱 Telegram
- Ask questions, list your library, and download documents straight from Telegram
- Send a file and it is ingested and indexed on the spot
- Replies are cleaned for Telegram and carry a "typing…" indicator while the model works

### 🔐 Multi-user, properly isolated
- PBKDF2-HMAC-SHA256 password hashing (240,000 rounds, per-user salts)
- Signed HttpOnly session cookies with a per-user **session epoch**
- Deleting, disabling, resetting a password, or logging out **actually kills existing sessions**
- Admins create users; there is **no public registration**
- Each user's library, files, and chat history are strictly their own
- Login rate limiting

### 💾 Built to keep your data
- Originals written to disk with `fsync` + atomic rename — never a temp file the OS can reap
- SQLite as the system of record for users, files, conversations, messages
- **Nightly backups to Cloudflare R2**, integrity-checked before upload
- Chat history replayable in the UI, per conversation

### 🛡️ Doesn't vanish on you
- A backup that has never been restored is just a hope. Every snapshot is verified with `PRAGMA quick_check` *before* it is uploaded, and 14 days are retained.

---

## 🚀 Quick start

### 1️⃣ Prerequisites

You need:

- 🐍 Python 3.10+
- 🔑 An [NVIDIA API key](https://build.nvidia.com/) for embeddings, OCR, and chat
- 📦 A [Qdrant](https://qdrant.tech/) instance — Cloud or self-hosted

### 2️⃣ Install

```bash
git clone https://github.com/Ferns1992/dococr-agent.git
cd dococr-agent
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3️⃣ Configure

```bash
cp .env.example .env
```

Then edit `.env`:

```ini
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxx
QDRANT_URL=https://your-cluster.cloud.qdrant.io
QDRANT_API_KEY=your-qdrant-key

# First admin, created on startup only if no admin exists yet
ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose-something-strong
```

> 🔒 **`.env` is gitignored. Never commit it.** Rotate any key that has ever appeared in a chat, a log, or a screenshot.

### 4️⃣ Run

```bash
./venv/bin/uvicorn app:app --host 0.0.0.0 --port 8077
```

Open **http://localhost:8077** and sign in.

> 📖 The PDF and image paths need a lightweight system library for image decoding. On Debian/Ubuntu: `apt-get install -y poppler-utils`

---

## ⚙️ Configuration

Everything is environment-driven. The full list lives in [`backend/.env.example`](backend/.env.example).

| Variable | Default | What it does |
|----------|---------|--------------|
| `NVIDIA_API_KEY` | — | 🔑 **Required.** Embeddings, OCR, and chat |
| `QDRANT_URL` | — | 📦 **Required.** Vector database endpoint |
| `QDRANT_API_KEY` | — | 🔑 Qdrant auth, if your instance needs it |
| `ADMIN_USERNAME` | `admin` | 👤 First admin's username |
| `ADMIN_PASSWORD` | — | 🔑 First admin's password. Change it after login |
| `TELEGRAM_BOT_TOKEN` | — | 💬 Bot token from @BotFather (enables the Telegram bot) |
| `TELEGRAM_ADMIN_IDS` | — | 👤 Comma-separated Telegram user IDs allowed to use the bot |
| `TELEGRAM_PASSCODE` | — | 🔑 Optional: lock the bot behind a passcode |
| `TELEGRAM_LOCK_MINUTES` | `60` | ⏳ Idle time after which the bot re-asks for the passcode |
| `CHAT_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b` | 🧠 Answer model |
| `EMBED_MODEL` | `nvidia/nemotron-3-embed-1b` | 🧬 Text embeddings |
| `OCR_MODEL` | `meta/llama-3.2-11b-vision-instruct` | 👁️ Image and scan OCR |
| `OCR_ENABLED` | `true` | Toggle OCR entirely |
| `TOP_K` | `6` | 🔍 Passages retrieved per question |
| `CHUNK_CHARS` | `1400` | ✂️ Chunk size for long documents |
| `MAX_UPLOAD_BYTES` | `26214400` | 📏 Upload ceiling (25 MB) |
| `COLLECTION_PREFIX` | `docchat` | 🏷️ Qdrant collection name prefix |

---

## 🔐 Answer modes in practice

The same question, three modes:

**📄 Documents** — asked *"What is Ollama?"* with a CV that only lists the word:

> The provided document lists "Ollama" as one of Fabian Fernandes's skills but does not define or describe what Ollama is `[1]`.

**🧠 + Knowledge** — same question, same document:

> Ollama is a tool for running large language models locally, often used with Docker for development and deployment **(not in your documents)**. The provided context lists Ollama as one of Fabian Fernandes's skills but does not define it `[1]`.

**🔬 Research** — same question, now with a web search:

> Ollama is an open-source platform for running and managing LLMs locally on your own hardware, with a CLI, a local REST API, and integrations with coding assistants `[W4]`. Models run on your machine, keeping data private and avoiding cloud latency `[W2][W3]`.

Same input. Three honestly different answers — and you can tell which is which every time.

---

## 📱 Telegram integration

The optional `tgbot.py` wraps the same backend in a **Telegram bot**, so you can
use the library without opening the browser. Only the user IDs in
`TELEGRAM_ADMIN_IDS` can talk to it.

### Setting it up

1. Talk to [@BotFather](https://t.me/BotFather) and create a bot to get a token.
2. Add the Telegram variables to `.env`:

   ```ini
   TELEGRAM_BOT_TOKEN=123456789:ABCdef-xyz
   TELEGRAM_ADMIN_IDS=123456789,987654321
   TELEGRAM_PASSCODE=     # optional: unlock required before first use
   ```

3. Run the bot: `./venv/bin/python tgbot.py`

The bot talks to the same FastAPI app, so it must be reachable from the host —
by default it calls `http://127.0.0.1:8077`.

### What it can do

- **Plain text** is asked as a question — answers are grounded and cited like the web UI
- **Files** (PDF, image, DOCX, TXT…) sent to the chat are ingested and indexed
- `/ask` or a plain question → answer from your library (citations included)
- `/sources` → list your documents
- `/stats` → backend health, chat and OCR models
- `/file <name>` or a natural-language request like *"send me the OpenRAG image"* → the document back to you
- `/start`, `/help` → command help

### Notes

- Replies are stripped of the model's raw markdown so they read cleanly in Telegram,
  and a **typing…** indicator shows while an answer is being generated.
- With `TELEGRAM_PASSCODE` set, the bot locks after `TELEGRAM_LOCK_MINUTES`
  of inactivity and requires the passcode to unlock.
- Compressed photos arrive from Telegram **without their original filename**, so
  they are stored under `photo-<timestamp>.jpg` unless you add a caption. Send an
  image as a **File** if you want the exact name kept.
- Nothing in the Telegram flow is anonymous: only listed admins can interact.

---

## 💾 Backups

Your data lives in three places, and they fail differently:

| Data | Where | Survives a disk failure? |
|------|-------|--------------------------|
| 📄 Original uploads | `backend/data/files/` on disk | ✅ Only via backup |
| 🗄️ Database | `backend/data/app.db` (SQLite) | ✅ Only via backup |
| 🧬 Vectors | Qdrant (hosted) | ✅ Yes, it's off-box |

That's why the backup script exists. It uses `sqlite3 .backup` rather than `cp`, because the app runs in **WAL mode** and a plain copy can capture a torn database.

### Setting it up

```bash
# 1. Configure an rclone remote (S3-compatible; Cloudflare R2 works)
rclone config

# 2. Install the script
install -m 0755 ops/docchat-backup.sh /usr/local/bin/docchat-backup.sh

# 3. Run it once, and check the manifest it leaves in the bucket
docchat-backup.sh
```

It verifies integrity **before** uploading, tars the uploads so the set is atomic, writes a self-describing manifest with restore instructions, and keeps 14 days.

### Restoring

Every backup includes a manifest with exact steps:

```bash
systemctl stop docchat
sqlite3 backend/data/app.db ".restore 'app.db.<stamp>'"
tar -C backend/data -xzf files.<stamp>.tar.gz
systemctl start docchat
```

---

## 🗂️ Project layout

```
dococr-agent/
├── 📄 backend/
│   ├── app.py           🌐 FastAPI app, routes, SSE streaming, auth middleware
│   ├── auth.py          🔐 Users, password hashing, sessions, epoch revocation
│   ├── db.py            🗄️ SQLite schema, permanent file storage
│   ├── store.py         🧬 Qdrant upsert, search, per-user scoping
│   ├── ingest.py        📥 File and URL ingestion pipeline
│   ├── extract.py       📄 PDF, image, text, HTML extraction
│   ├── nvidia_client.py 🤖 Embeddings, OCR, streaming chat with retries
│   ├── research.py      🔬 Web search for Research mode
│   ├── tgbot.py         📱 Optional Telegram bot
│   ├── config.py        ⚙️ Environment configuration
│   └── .env.example     📋 Every setting, documented
├── 🎨 frontend/
│   ├── index.html       💬 Main chat UI
│   ├── login.html       🔑 Login page
│   ├── admin.html       👥 Admin panel
│   ├── app.js           ⚙️ Chat, history, answer modes
│   └── style.css        🎨 Styling
└── 🛠️ ops/
    └── docchat-backup.sh 💾 Nightly backup to object storage
```

---

## 🐛 Troubleshooting

<details>
<summary><b>Health check says "not configured"</b></summary>

`/api/health` tells you exactly what is missing. Usually `NVIDIA_API_KEY` isn't loaded — confirm the file is named `.env` and sits next to `app.py`, not in `frontend/`.
</details>

<details>
<summary><b>Uploads fail but the site loads</b></summary>

Check disk space. Then confirm the upload ceiling: `MAX_UPLOAD_BYTES` defaults to 25 MB.
</details>

<details>
<summary><b>Every answer says "I could not find anything"</b></summary>

The library is empty for that user, or the query is too far from any indexed text. Try Research mode, which searches the web instead. Users only ever see their own documents — that's deliberate isolation, not a bug.
</details>

<details>
<summary><b>An answer came back empty once</b></summary>

Hosted model endpoints occasionally truncate a stream. The client detects an empty response and retries automatically, so this resolves itself. If it's persistent, check the service logs.
</details>

<details>
<summary><b>Research mode finds nothing</b></summary>

It uses Wikipedia's API and DuckDuckGo without an API key, so it needs outbound HTTPS. Confirm the host can reach the internet, and prefer **Research** over Documents for anything time-sensitive.
</details>

---

## 📄 License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

**Made for anyone tired of copying documents into a chatbot and hoping.**

⭐ If this saved you a copy-paste, consider starring it.

</div>
