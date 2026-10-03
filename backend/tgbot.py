import json
import logging
import os
import pathlib
import re
import sys
import threading
import time

import httpx

# Last document delivered per chat, so follow-ups like "explain the" can be
# anchored on the file the user actually meant (see do_ask).
LAST_SENT_FILE: dict = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = pathlib.Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env")
except Exception:
    pass

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
PASSCODE = os.getenv("TELEGRAM_PASSCODE", "")
LOCK_MINUTES = int(os.getenv("TELEGRAM_LOCK_MINUTES", "60") or "60")
APP = "http://127.0.0.1:8077"
BOT_WHO = "https://api.telegram.org"

# Per-chat inactivity lock. last_active tracks when the admin last interacted;
# after LOCK_MINUTES with no message the session is locked and the next message
# must carry the passcode (unless it is kept in .env as "" to disable).
MAX_WRONG_PASSCODE = 3
BLOCK_MINUTES = 60
if PASSCODE:
    _last_active: dict = {}
    _pending: dict = {}      # chat_id -> last raw update while locked
    _prompted: dict = {}     # chat_id -> lock message already shown
    _failed: dict = {}       # chat_id -> consecutive wrong passcode count
    _blocked_until: dict = {}  # chat_id -> time.time() when block expires
else:
    _last_active = _pending = _prompted = _failed = _blocked_until = None


def tg(method: str, **params):
    if "json" in params:
        r = httpx.post(f"{BOT_WHO}/bot{TOKEN}/{method}", json=params["json"], timeout=120)
    elif "files" in params:
        files = params["files"]
        data = {k: v for k, v in params.items() if k != "files"}
        r = httpx.post(f"{BOT_WHO}/bot{TOKEN}/{method}", data=data, files=files, timeout=300)
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
    if cmd == "/file":
        q = text[len("/file"):].split("@", 1)[0].strip()
        if not q:
            send_text(chat_id, "Usage: /file <name or part> - sends that document to you.")
            return
        send_a_file(chat_id, q, app)
        return
    if cmd == "/ask":
        q = text[len("/ask"):].split("@", 1)[0].strip()
        if not q:
            send_text(chat_id, "Usage: /ask <your question>")
            return
        do_ask(chat_id, q, app)
        return

    # Natural language file request? e.g. "send me the OpenRAG image",
    # "download resume.pdf", "can you show me the workflow diagram?"
    q = try_file_request(text)
    if q:
        send_a_file(chat_id, q, app)
        return

    do_ask(chat_id, text, app)


# Bare conversational continuations that must never be treated as file
# requests: "i want explain", "tell me more", "show me it" etc. refer to the
# conversation, not to a document, and should fall through to chat memory.
_FOLLOWUP_WORDS = {
    "explain", "more", "it", "that", "this", "those", "these", "know",
    "see", "tell", "show", "continue", "again", "info", "information",
    "details", "detail", "describe", "what", "why", "how", "the file",
}


def try_file_request(text: str) -> str:
    """Return a source query when the message reads like a request for a file."""
    t = text.strip().rstrip(".!?,")
    low = t.lower()
    # "explain this file", "describe the resume", "what is in openrag" are
    # questions about a document, not requests to receive it as a file. Let
    # the normal chat path answer from the retrieved content instead of the
    # file-send path just dumping the attachment.
    if any(v in low for v in (
        "explain", "describe", "what is in ", "what's in ", "what does it say",
        "tell me about", "tell me more about", "detail", "more about",
    )):
        return ""
    # peel politeness so "please send me X", "can you send the X" work
    for lead in ("please ", "pls ", "can you ", "could you ", "kindly ", "hey ", "hi ", "hello "):
        if low.startswith(lead):
            low = low[len(lead):]
            t = t[len(lead):].strip()
    prefixes = (
        "send me ",
        "send ",
        "download ",
        "give me ",
        "gimme ",
        "fetch ",
        "show me ",
        "attach ",
        "return the ",
        "get me the ",
        "can you send",
        "can i get ",
        "can i have ",
        "can i fetch ",
        "could i get ",
        "can i see a preview of ",
        "can i see the preview of ",
        "can i see ",
        "show a preview of ",
        "show the preview of ",
        "preview of ",
        "the preview of ",
        "preview ",
        "i want the ",
        "i want ",
        "id like ",
        "i\'d like ",
        "please send",
    )
    blob = " " + low + " "
    has_verb = any(p in low for p in prefixes) or any(
        v in blob for v in (" the file ", " this file ", " that file ", " file please", " pd f")
    )
    looks_like_filename = low.rstrip().endswith(
        (".png", ".jpg", ".jpeg", ".pdf", ".docx", ".doc", ".txt", ".md", ".html")
    )
    if not (has_verb or looks_like_filename):
        return ""
    rest = t
    for p in prefixes:
        if low.startswith(p):
            rest = t[len(p):].strip()
            break
    rest = rest.strip(" \t\n:;.,!?\"'")
    if not rest:
        return ""
    # "i want explain" / "send me more" are conversational continuations
    # (memory follow-ups), not file requests. Bail so the message reaches
    # the normal chat path.
    if rest.lower() in _FOLLOWUP_WORDS:
        return ""
    # if a token carries a real file extension, prefer everything up to and
    # including that token: "Fabian Milton Fernandes Resume.pdf this" -> the PDF
    mt = re.search(r"([^ ]+\.(?:png|jpg|jpeg|pdf|docx?|txt|md|html)(?:[^ ]*))$", rest, re.I)
    if mt:
        rest = rest[:mt.start(1)].strip() + " " + mt.group(1).split(" ")[-1].rstrip(".,;:!?\"'")
        rest = rest.strip()
    # strip trailing filler like "file", "please", "for me", "the image"
    for chase in (" please", " the file", " the image", " the document",
                  " image", " the infographic", " infographic", " document",
                  " for me", " from the library", " file", " this", " that"):
        if rest.lower().endswith(chase):
            rest = rest[:-len(chase)].strip()
            if not rest:
                return ""
    # drop a leading "the "/"a "/"an " and trailing format words so the
    # substring match against the source name succeeds
    low = rest.lower()
    for lead in ("the ", "a ", "an "):
        if low.startswith(lead):
            rest = rest[len(lead):].strip()
            break
    rest = re.sub(r"\s+(png|jpg|jpeg|pdf|docx?|txt|md|html|diagram|image)$", "", rest, flags=re.I).strip()
    return rest


_REFERENTIAL_RE = re.compile(
    r"\b(the file|this file|that file|the document|this document|that document|"
    r"the resume|the pdf|the image|the diagram|the infographic|the one|that one|"
    r"the thing|the stuff|it|them|these|those)\b|"
    r"\b(explain|describe|tell me about|what about the)\s+?the?\s*$",
    re.IGNORECASE,
)


def _resolve_followup(chat_id: int, question: str) -> str:
    """Anchor a vague follow-up on the last file we sent for this chat."""
    last = LAST_SENT_FILE.get(chat_id)
    if not last:
        return question
    ql = question.lower()
    # Question names a file already -> nothing to resolve.
    if last.lower() in ql:
        return question
    # Only resolve when the question *refers* to a file without naming it.
    if not _REFERENTIAL_RE.search(ql):
        return question
    # Bare pronouns like "it"/"them" are ambiguous; require a file-ish or
    # explanation-like verb so we don't hijack ordinary chat.
    looks_like_followup = any(w in ql for w in (
        "explain", "describe", "about", "detail", "tell me", "what is", "what's",
        "what about", "more", "content", "summar", "send", "give me", "again",
    ))
    if not looks_like_followup:
        return question
    return f"[You sent file: {last}.] {question}"


def do_ask(chat_id: int, question: str, app: AppClient):
    # Native Telegram status: "typing…" under the bot's name at the top of the
    # chat. Called now and re-sent every 4s while generating (Telegram clears
    # it ~5s after the last action AND whenever a bot message arrives, so the
    # heartbeat below keeps it alive across the placeholder edit as well).
    tg("sendChatAction", chat_id=chat_id, action="typing")

    # A follow-up like "explain the", "what about it", "tell me about the one
    # you sent" refers to the last file we delivered. The file-send path
    # never writes to the app conversation, so the RAG has no memory of it;
    # resolve the reference here and pass the file name along explicitly.
    question = _resolve_followup(chat_id, question)

    # The model stream can take a while. Run it on a dedicated thread so the
    # heartbeat keeps the native typing status alive. Post a lightweight "…"
    # placeholder too and edit it into the real answer when done, so there is
    # visible feedback on clients that do not render bot chat actions.
    result = {}

    def _run():
        try:
            result["answer"], result["sources"] = app.chat(question, mode="blend")
        except Exception as exc:  # pragma: no cover
            result["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    placeholder = tg("sendMessage", chat_id=chat_id, text="…")
    mid = placeholder["result"]["message_id"] if placeholder and placeholder.get("ok") else None

    while worker.is_alive():
        worker.join(timeout=4)
        if worker.is_alive():
            try:
                tg("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass

    if "error" in result:
        fallback = f"Chat request failed: {result['error']}"
        if mid:
            try:
                tg("editMessageText", chat_id=chat_id, message_id=mid, text=fallback)
                return
            except Exception:
                pass
        send_text(chat_id, fallback)
        return

    answer, sources = result.get("answer", ""), result.get("sources", [])
    if not answer.strip():
        answer = "I could not produce an answer for that. Try rephrasing."
    reply = _strip_markdown(answer.strip())
    if sources:
        uniq = []
        for s in sources:
            if s not in uniq:
                uniq.append(s)
        reply += ("\n\n📄 Sources: " + " • ".join(uniq[:5]))

    chunks = split_messages(reply)
    if mid:
        try:
            tg("editMessageText", chat_id=chat_id, message_id=mid, text=chunks[0])
            for c in chunks[1:]:
                send_text(chat_id, c)
            return
        except Exception:
            pass
    send_text(chat_id, reply)


def _strip_markdown(text: str) -> str:
    """Strip markdown / citation artifacts before sending to Telegram.

    The model replies with **bold**, ### headers, `code`, | tables |, and
    [1] [2] citation markers. Telegram only renders those when parse_mode is
    set; we send plain text, so they must be removed or the user sees raw
    stars, hashes, pipes and brackets."""
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)          # **bold** -> bold
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)  # *em* -> em
    text = re.sub(r"__([^_]+)__", r"\1", text)              # __bold__ -> bold
    text = re.sub(r"`([^`]+)`", r"\1", text)                # `code` -> code
    text = re.sub(r"\[([0-9]+)\]", "", text)                # [1] -> ""
    text = re.sub(r"\[W[0-9]+\]", "", text)                 # [W1] -> ""
    # Headings glued to preceding text ("…:### Files") -> split onto own line.
    text = re.sub(r"(?<=\S)#{1,6}\s*", "\n", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|.*\|$", "", text, flags=re.MULTILINE)  # | table row ->
    text = re.sub(r"^\s*\|[-:|\s]+\|$", "", text, flags=re.MULTILINE)  # |---|
    text = re.sub(r"\s*\n?-{3,}\s*\n?", "\n", text)         # --- dividers
    text = re.sub(r"^=+$", "", text, flags=re.MULTILINE)    # === dividers
    text = re.sub(r"[ \t]{2,}", " ", text)                  # double spaces
    text = re.sub(r"\s*\n{3,}\s*\n?", "\n\n", text)         # extra blank lines
    return text.strip()


def _sendable(s: dict) -> bool:
    """A source can only be sent to Telegram if it has a stored file.

    URL-origin ("paperclip.git") and other sources without a stored_path have
    no bytes backing them, so /api/sources/{id}/file 404s. Skip those."""
    return bool(s.get("stored_path")) or (s.get("origin") or "") != "url"


def send_a_file(chat_id: int, query: str, app: AppClient):
    srcs = app.get("/api/sources")
    if srcs.status_code != 200:
        send_text(chat_id, "Could not list sources.")
        return
    ql = query.lower()
    hits = [s for s in srcs.json().get("sources", [])
            if _sendable(s)
            and (ql in (s.get("filename") or s.get("source_name") or "").lower()
                 or ql in (s.get("source_name") or "").lower()
                 or ql == (s.get("source_id") or "").lower())]
    if not hits:
        # token-overlap fallback: "a hold of this file openrag" -> OpenRAG
        skip = {
            "a","an","the","this","that","these","those","of","to","for","with",
            "get","gets","gotten","gimme","me","my","i","i'm","id","can","could",
            "would","want","wants","please","pls","file","files","hold","way","there",
            "is","are","was","am","have","has","send","sent","show","download",
            "document","docs","image","images","diagram","infographic","list","name",
            "on","at","by","and","or","but","in","it","its","you","your"
        }
        qtoks = [w for w in re.findall(r"[a-z0-9]+", ql) if len(w) >= 3 and w not in skip]
        scored = []
        for s in srcs.json().get("sources", []):
            if not _sendable(s):
                continue
            name = (s.get("filename") or s.get("source_name") or "").lower()
            if not qtoks:
                continue
            score = sum(1 for w in set(qtoks) if w in name)
            if score > 0:
                scored.append((score, s))
        if scored:
            scored.sort(key=lambda x: (-x[0], (x[1].get("source_name") or "").lower()))
            best_score = scored[0][0]
            hits = [s for sc, s in scored if sc == best_score]
            if len(hits) == 1 and len(qtoks) >= 2:
                # Only one file matches every word; include partial matches too
                # so "openrag" still finds the diagram, the png, the doc, ...
                hits = [s for sc, s in scored]
    if not hits and qtoks:
        # content fallback: ask the app which indexed text is most relevant
        try:
            search = app.get("/api/search", params={"query": query, "top_k": 20})
            if search.status_code == 200:
                found = [(s.get("source_name"), s.get("source_id"), s.get("score"))
                         for s in search.json().get("sources", [])]
                if found:
                    by_name = {s.get("source_name"): s for s in srcs.json().get("sources", [])}
                    by_name.update({s.get("filename"): s for s in srcs.json().get("sources", [])})
                    resolved = []
                    for name, sid, score in found:
                        s = by_name.get(name)
                        if s is None:
                            s = next((x for x in srcs.json().get("sources", [])
                                      if (x.get("source_id") or x.get("id")) == sid), None)
                        if s and _sendable(s):
                            resolved.append((score, s))
                    if resolved:
                        resolved.sort(key=lambda x: -(x[0] or 0.0))
                        hits = [s for _, s in resolved[:8]]
        except Exception as exc:
            logging.warning("content search failed: %s", exc)
    if not hits:
        if not qtoks:
            names = [s.get("source_name") or s.get("filename") or "?"
                     for s in srcs.json().get("sources", [])]
            listing = "\n".join(f"- {n}" for n in names) or "(no files yet)"
            send_text(chat_id,
                      f"'{query}' is too vague for me to match a file. I can send one of:\n{listing}\n\n"
                      f"e.g. \"send openrag\" or \"get the resume\". Try /sources too.")
        else:
            send_text(chat_id, f"No source matches '{query}'. Try /sources to see names.")
        return
    if len(hits) > 1:
        send_text(chat_id, f"Matched {len(hits)} sources; sending all of them.")
    sent_ok = []
    failures = []
    for s in hits:
        sid = s.get("source_id") or s.get("id")
        name = s.get("source_name") or s.get("filename") or "?"
        tg("sendChatAction", chat_id=chat_id, action="upload_document")
        try:
            r = app.get(f"/api/sources/{sid}/file")
            if r.status_code != 200:
                failures.append(f"{name} (HTTP {r.status_code})")
                logging.warning("could not fetch %s: %s", name, r.status_code)
                continue
            files = {"document": (s.get("filename") or s.get("source_name") or "file",
                                  r.content, s.get("mime") or "application/octet-stream")}
            resp = tg("sendDocument", chat_id=chat_id, files=files)
            if resp.get("ok"):
                sent_ok.append(name)
                logging.info("sent %s to chat %s", name, chat_id)
            else:
                failures.append(f"{name} (Telegram rejected: "
                                + str(resp.get("description", "unknown error")) + ")")
        except Exception as exc:
            failures.append(f"{name} ({exc})")
            logging.exception("sendDocument failed for %s", name)
        time.sleep(0.5)
    if sent_ok and failures:
        send_text(chat_id, "Sent " + ", ".join(sent_ok) + ". Could not send: "
                  + "; ".join(failures) + ".")
    elif sent_ok:
        send_text(chat_id, "Sent " + ", ".join(sent_ok) + ".")
    else:
        send_text(chat_id, "None of the matched sources could be sent: "
                  + "; ".join(failures) + ".")
    if sent_ok:
        LAST_SENT_FILE[chat_id] = sent_ok[-1]
    logging.warning("sent %d, failed %d of %d candidates",
                    len(sent_ok), len(failures), len(hits))


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


def _dispatch(chat_id: int, msg: dict, app: AppClient):
    """Route one inbound message to the matching handler."""
    text = (msg.get("text") or "").strip()
    if text:
        handle_command(chat_id, text, app)
        return
    doc = msg.get("document")
    if doc:
        name = doc.get("file_name") or "document"
        mime = doc.get("mime_type") or "application/octet-stream"
        handle_upload(chat_id, doc.get("file_id"), name, mime, app)
        return
    photo = msg.get("photo")
    if photo:
        largest = photo[-1]
        cap = (msg.get("caption") or "").strip()
        if cap:
            # Telegram sends no filename with compressed photos, so the caption
            # is the only chance to name it meaningfully. Use it if it is short
            # and file-ish; otherwise keep the caption as a human label.
            if len(cap) <= 64 and re.fullmatch(r"[\w\- .+()\[\]]+\.?[a-z0-9]*", cap, re.I):
                name = cap if "." in cap else cap + ".jpg"
            elif len(cap) <= 64:
                name = re.sub(r"[^\w\-]+", "_", cap).strip("_") + ".jpg"
            else:
                name = re.sub(r"[^\w\-]+", "_", cap)[:60].strip("_") + ".jpg"
        else:
            # Telegram stripped the original filename (compressed photo), so
            # fall back to a timestamped label to keep uploads distinguishable.
            name = "photo-" + time.strftime("%Y%m%d-%H%M%S") + ".jpg"
        handle_upload(chat_id, largest.get("file_id"), name, "image/jpeg", app)


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

        if _pending is not None:
            now = time.time()
            idle = now - _last_active.get(chat_id, 0)
            if idle > LOCK_MINUTES * 60:
                blocked_until = _blocked_until.get(chat_id, 0)
                if blocked_until and now < blocked_until:
                    # Session is blocked: ignore everything until expiry.
                    _pending[chat_id] = msg
                    if not _prompted.get(chat_id):
                        _prompted[chat_id] = True
                        mins = max(1, int((blocked_until - now) // 60))
                        send_text(
                            chat_id,
                            "Too many wrong passcodes. Your session is blocked for "
                            f"about {mins} more minute{'s' if mins != 1 else ''}.",
                        )
                    continue
                if text == PASSCODE:
                    _last_active[chat_id] = now
                    _prompted[chat_id] = False
                    _failed[chat_id] = 0
                    _blocked_until[chat_id] = 0
                    pending = _pending.pop(chat_id, None)
                    if pending is not None:
                        send_text(chat_id, "Unlocked. Answering your last request.")
                        _dispatch(chat_id, pending, app)
                    else:
                        send_text(chat_id, "Unlocked.")
                else:
                    _pending[chat_id] = msg
                    _failed[chat_id] = _failed.get(chat_id, 0) + 1
                    if _failed[chat_id] >= MAX_WRONG_PASSCODE:
                        _blocked_until[chat_id] = now + BLOCK_MINUTES * 60
                        _failed[chat_id] = 0
                        _prompted[chat_id] = False
                        send_text(
                            chat_id,
                            "Wrong passcode. Too many tries — your session is now "
                            f"blocked for {BLOCK_MINUTES} minutes.",
                        )
                    elif not _prompted.get(chat_id):
                        _prompted[chat_id] = True
                        send_text(
                            chat_id,
                            "Session timed out after "
                            f"{LOCK_MINUTES} minute{'s' if LOCK_MINUTES != 1 else ''} of "
                            "inactivity. Send your passcode to unlock.",
                        )
                    else:
                        left = MAX_WRONG_PASSCODE - _failed[chat_id]
                        send_text(
                            chat_id,
                            f"Wrong passcode. {left} attempt{'s' if left != 1 else ''} "
                            f"left before the session is blocked for {BLOCK_MINUTES} "
                            "minutes.",
                        )
                continue
        _last_active[chat_id] = time.time()
        _dispatch(chat_id, msg, app)
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