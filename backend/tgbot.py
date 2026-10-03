import json
import logging
import os
import pathlib
import sys
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = pathlib.Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except Exception:
    pass

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
APP = "http://127.0.0.1:8077"
BOT_WHO = "https://api.telegram.org"


def tg(method: str, **params):
    if "json" in params:
        r = httpx.post(f"{BOT_WHO}/bot{TOKEN}/{method}", json=params["json"], timeout=120)
    elif "files" in params:
        r = httpx.post(f"{BOT_WHO}/bot{TOKEN}/{method}", files=params["files"], timeout=300)
    else:
        r = httpx.post(f"{BOT_WHO}/bot{TOKEN}/{method}", json=params, timeout=120)
    return r.json()


def send_text(chat_id: int, text: str):
    for chunk in split_messages(text):
        tg("sendMessage", chat_id=chat_id, text=chunk)
        time.sleep(0.15)


def split_messages(text: str, limit: int = 3900):
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > 200 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


class AppClient:
    """Holds the admin session cookie and re-attaches it on 401."""

    def __init__(self):
        self.client = httpx.Client(base_url=APP, timeout=300)
        self.username = os.getenv("ADMIN_USERNAME", "admin")
        self.password = os.getenv("ADMIN_PASSWORD", "")
        self._login()

    def _login(self):
        self.client.cookies.clear()
        r = self.client.post("/api/login", json={"username": self.username, "password": self.password})
        r.raise_for_status()

    def get(self, path: str, **kw):
        r = self.client.get(path, **kw)
        if r.status_code == 401:
            self._login()
            r = self.client.get(path, **kw)
        return r

    def post(self, path: str, **kw):
        r = self.client.post(path, **kw)
        if r.status_code == 401:
            self._login()
            r = self.client.post(path, **kw)
        return r

    def chat(self, question: str, mode: str = "blend"):
        """Consume the SSE stream and return (answer_text, source_names)."""
        payload = {"question": question, "mode": mode, "conversation_id": "telegram"}
        answer_parts = []
        sources = []
        try:
            with self.client.stream("POST", "/api/chat", json=payload) as r:
                if r.status_code == 401:
                    self._login()
                    with self.client.stream("POST", "/api/chat", json=payload) as r2:
                        return self._read_sse(r2)
                return self._read_sse(r)
        except Exception as exc:  # pragma: no cover
            return f"Chat request failed: {exc}", []

    @staticmethod
    def _read_sse(r):
        answer_parts = []
        sources = []
        event = ""
        for raw in r.iter_lines():
            if not raw:
                continue
            if raw.startswith("event:"):
                event = raw.split(":", 1)[1].strip()
            elif raw.startswith("data:"):
                try:
                    data = json.loads(raw[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                if event == "sources":
                    for hit in data.get("hits", []):
                        sources.append(hit.get("source_name") or hit.get("source_id"))
                elif event == "delta":
                    answer_parts.append(data.get("text", ""))
                elif event == "error":
                    answer_parts.append(f"[error] {data.get('message', '')}")
        return "".join(answer_parts), sources


def file_ingest(app: AppClient, file_bytes: bytes, name: str, mime: str):
    r = app.post(
        "/api/ingest",
        files={"file": (name, file_bytes, mime)},
    )
    if r.status_code >= 400:
        return f"Upload rejected ({r.status_code}): {r.text[:200]}"
    job = r.json()
    job_id = job.get("job_id") or job.get("id")
    prev_step = None
    for _ in range(600):
        time.sleep(2)
        st = app.get(f"/api/jobs/{job_id}")
        if st.status_code != 200:
            time.sleep(2)
            continue
        j = st.json()
        step = j.get("step", "")
        if step != prev_step:
            prev_step = step
        if j.get("status") == "done":
            res = j.get("result") or {}
            return "done", res.get("source_id"), res.get("chunks"), res.get("ocr_chars")
        if j.get("status") == "error":
            return "error", None, None, j.get("error")
    return "error", None, None, "timed out waiting for the ingest job"


def handle_command(chat_id: int, text: str, app: AppClient):
    cmd = text.split()[0].lower().split("@")[0]
    if cmd in ("/start", "/help"):
        send_text(
            chat_id,
            "DocChat bot - admin only\n\n"
            "/ask <question>  - answer from your document library (citations)\n"
            "/sources         - list your documents\n"
            "/stats           - system health\n\n"
            "Or send a file (PDF, image, DOCX, TXT...) and I'll add it to the library.\n"
            "Plain text messages count as questions.",
        )
        return
    if cmd == "/stats":
        try:
            h = app.get("/api/health")
            j = h.json().get("health", h.json()) if h.status_code == 200 else {}
            ok = j.get("ok", j.get("status"))
            send_text(
                chat_id,
                f"DocChat health: ok={ok}\nchat={j.get('models', {}).get('chat')}\n"
                f"ocr={j.get('models', {}).get('ocr')}",
            )
        except Exception as exc:
            send_text(chat_id, f"stats failed: {exc}")
        return
    if cmd == "/sources":
        r = app.get("/api/sources")
        if r.status_code != 200:
            send_text(chat_id, "Could not list sources.")
            return
        src = r.json().get("sources", [])
        if not src:
            send_text(chat_id, "No documents in the library yet.")
            return
        lines = [f"- {s.get('source_name')} ({s.get('source_type')})" for s in src]
        send_text(chat_id, "Your documents:\n" + "\n".join(lines))
        return
    if cmd == "/ask":
        q = text[len("/ask"):].split("@", 1)[0].strip()
        if not q:
            send_text(chat_id, "Usage: /ask <your question>")
            return
        do_ask(chat_id, q, app)
        return

    do_ask(chat_id, text, app)


def do_ask(chat_id: int, question: str, app: AppClient):
    tg("sendChatAction", chat_id=chat_id, action="typing")
    answer, sources = app.chat(question, mode="blend")
    if not answer.strip():
        answer = "I could not produce an answer for that. Try rephrasing."
    reply = answer.strip()
    if sources:
        uniq = []
        for s in sources:
            if s not in uniq:
                uniq.append(s)
        reply += "\n\nSources: " + ", ".join(uniq[:5])
    send_text(chat_id, reply)


def handle_upload(chat_id: int, file_id: str, name: str, mime: str, app: AppClient):
    tg("sendChatAction", chat_id=chat_id, action="typing")
    try:
        info = tg("getFile", file_id=file_id)
        fp = (info.get("result") or {}).get("file_path")
        if not fp:
            send_text(chat_id, f"Could not retrieve the file ({name}).")
            logging.warning("getFile returned no file_path: %s", info)
            return
        r = httpx.get(f"{BOT_WHO}/file/bot{TOKEN}/{fp}", timeout=180)
        if r.status_code != 200:
            send_text(chat_id, f"Could not download the file from Telegram ({name}).")
            logging.warning("file download failed: status=%s", r.status_code)
            return
    except Exception as exc:
        send_text(chat_id, f"Could not download the file ({name}): {exc}")
        logging.exception("telegram download failed")
        return
    status, source_id, chunks, detail = file_ingest(app, r.content, name, mime)
    if status == "done":
        msg = f"Added '{name}' to your library"
        if chunks:
            msg += f" ({chunks} sections indexed)"
        if source_id:
            msg += f".\nID: {source_id}"
        send_text(chat_id, msg)
        logging.info("ingested %s -> %s (%s chunks)", name, source_id, chunks)
    else:
        send_text(chat_id, f"Failed to ingest '{name}': {detail}")
        logging.warning("ingest failed for %s: %s", name, detail)


def poll_once(app: AppClient, offset):
    try:
        up = tg("getUpdates", offset=offset or 1, timeout=50, allowed_updates=["message"])
    except Exception:
        return offset
    for u in up.get("result", []):
        offset = u["update_id"] + 1
        msg = u.get("message")
        if not msg:
            continue
        chat_id = msg["chat"]["id"]
        fid = msg.get("from", {}).get("id")
        if fid not in ADMIN_IDS:
            send_text(chat_id, "This bot is private and restricted to its owner.")
            continue
        text = (msg.get("text") or "").strip()
        if text:
            handle_command(chat_id, text, app)
            continue
        doc = msg.get("document")
        if doc:
            name = doc.get("file_name") or "document"
            mime = doc.get("mime_type") or "application/octet-stream"
            handle_upload(chat_id, doc.get("file_id"), name, mime, app)
            continue
        photo = msg.get("photo")
        if photo:
            largest = photo[-1]
            handle_upload(chat_id, largest.get("file_id"), "photo.jpg", "image/jpeg", app)
            continue
    return offset


def main():
    if not TOKEN:
        sys.stderr.write("TELEGRAM_BOT_TOKEN is not set in .env\n")
        sys.exit(1)
    if not ADMIN_IDS:
        sys.stderr.write("TELEGRAM_ADMIN_IDS is not set in .env\n")
        sys.exit(1)
    app = AppClient()
    offset = None
    while True:
        offset = poll_once(app, offset)
        time.sleep(0.5)


if __name__ == "__main__":
    main()