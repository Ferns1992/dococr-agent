import asyncio
import json
import os
import time
import uuid
from urllib.parse import quote
from typing import List, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from qdrant_client import models

import auth
import config
import db
import extract
import ingest
import nvidia_client
import research
import store

async def _read_json(request) -> dict:
    """Read a JSON request body. Must be awaited: Request.body() is a coroutine."""
    try:
        raw = await request.body()
        return json.loads(raw or b"{}")
    except Exception:
        return {}


app = FastAPI(title="DocChat", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# The first admin is created on startup only if no admin exists yet. The
# password is read from the environment so no credential is ever committed to
# source; change it once you are logged in and it stops mattering.
ADMIN_BOOTSTRAP = {
    "username": os.getenv("ADMIN_USERNAME", "admin"),
    "password": os.getenv("ADMIN_PASSWORD", ""),
}

# /icon.png is here because the login page itself references it; without a
# session the request was redirected back to /login and the browser tried to
# parse HTML as an image.
PUBLIC_PATHS = {
    "/login", "/login.html", "/login.css", "/login.js",
    "/favicon.ico", "/icon.png",
}
# /admin and /admin.js are deliberately NOT public: the session cookie is
# required, and the page itself only shows what the API will let you fetch.


def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return True
    # /api/login and /api/health must work without a session, otherwise
    # nobody could ever obtain one.
    return path in ("/api/health", "/api/login")


@app.middleware("http")
async def require_auth(request, call_next):
    """Gate everything behind a session cookie, except the login page.

    Requests that are API calls get a 401 so the SPA can redirect; document
    requests are redirected to /login so a bookmarked URL still works.
    """
    path = request.url.path
    if not _is_public(path):
        token = request.cookies.get(auth.COOKIE_NAME, "")
        user = auth.user_from_token(token) if token else None
        if user is not None and not user["is_active"]:
            user = None
        request.state.user = user
        if user is None:
            if path.startswith("/api/"):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "authentication required"}, status_code=401)
            from fastapi.responses import RedirectResponse

            return RedirectResponse("/login", status_code=302)
    else:
        request.state.user = None

    response = await call_next(request)
    if request.url.path == "/api/login" and response.status_code == 200:
        pass
    return response


def current_user(request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "authentication required")
    return user


def require_admin(request):
    user = current_user(request)
    if user["role"] != auth.ROLE_ADMIN:
        raise HTTPException(403, "admin access required")
    return user


def _client_ip(request) -> str:
    return request.client.host if request.client else "unknown"


_failed_logins: dict = {}


@app.post("/api/login")
async def api_login(request: Request):
    """Password login. There is no public registration by design."""
    body = await _read_json(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    ip = _client_ip(request)
    now = time.time()
    strikes = _failed_logins.setdefault(ip, [])
    strikes[:] = [t for t in strikes if now - t < 900]
    if len(strikes) >= 8:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")

    try:
        session = auth.login(username, password)
    except auth.AuthError as exc:
        strikes.append(now)
        raise HTTPException(401, str(exc)) from exc

    _failed_logins.pop(ip, None)
    response = JSONResponse({"user": session["user"], "expires_at": session["expires_at"]})
    response.set_cookie(
        auth.COOKIE_NAME,
        session["token"],
        max_age=auth.TOKEN_TTL,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return response


_GROUNDING_RULES = """\
- Cite every claim taken from a passage with its bracketed number, for example [2]. \
Place the citation at the end of the sentence it supports.
- When passages disagree, say so and cite both.
- Be concise and concrete. No preamble, no restating the question.
"""

# Strict mode: the library is the only permitted source. This is the right
# default when the answer has to be traceable to something the user uploaded.
SYSTEM_PROMPT = (
    "You are a helpful assistant that answers questions about a personal document library.\n\n"
    "Rules:\n"
    "- Answer only from the numbered context passages provided. They are the retrieved "
    "excerpts, not a conversation.\n"
    "- If the passages do not contain the answer, say plainly that the documents do not "
    "cover it. Do not guess or fill in from general knowledge.\n"
    + _GROUNDING_RULES
)

# Blend mode: the documents are the primary source, but the model may add what it
# already knows when they are silent. Anything not backed by a passage is marked so
# the user can tell at a glance which claims are traceable and which are not.
SYSTEM_PROMPT_BLEND = (
    "You are a helpful assistant that answers questions using a personal document library "
    "as its primary source, supplemented by your own general knowledge.\n\n"
    "Rules:\n"
    "- Use the numbered context passages first. They are the retrieved excerpts, not a "
    "conversation.\n"
    "- If the passages only partly cover the question, answer that part from the passages "
    "and add the rest from your own knowledge.\n"
    "- If the passages do not cover the question at all, answer from your own knowledge.\n"
    "- Mark any claim that does not come from a passage by ending that sentence with "
    "(not in your documents). Omit that marker entirely for claims a passage supports, so "
    "the two are always distinguishable.\n"
    "- Never present a personal detail, figure, or claim about the user as general "
    "knowledge. Anything specific to this person must come from a passage.\n"
    "- If you are unsure, say so rather than inventing detail.\n"
    + _GROUNDING_RULES
)

# Blend mode with nothing retrieved at all.
SYSTEM_PROMPT_NO_CONTEXT = (
    "You are a helpful assistant. The user's document library returned nothing relevant to "
    "this question, so answer from your own general knowledge.\n\n"
    "Rules:\n"
    "- Start your reply with the single line: Not in your documents.\n"
    "- Then answer the question directly and usefully.\n"
    "- Be clear about the limits of what you know and do not invent specifics.\n"
    "- Be concise. No preamble."
)

# Research mode: web sources are numbered [W1], [W2] alongside the user's own
# documents numbered [1], [2]. Keeping the prefixes distinct means a citation
# always tells the reader where a claim actually came from.
SYSTEM_PROMPT_RESEARCH = (
    "You are a research assistant. You answer using two kinds of source, and you must "
    "always tell the reader which is which.\n\n"
    "The user's own documents are numbered [1], [2], and so on. Public web pages you "
    "retrieved are numbered [W1], [W2], and so on, with their URL in the citation.\n\n"
    "Rules:\n"
    "- Prefer the user's documents when they actually answer the question; they are the "
    "more authoritative source for anything about them.\n"
    "- Use the web sources to explain, expand, or supply current facts the documents do "
    "not cover. This is the main reason research mode exists.\n"
    "- Every factual claim must carry a citation: [2] for a document, [W1] for a web "
    "source. Put it at the end of the sentence it supports.\n"
    "- Never invent a citation. If a claim has no supporting source, either drop it or "
    "mark it as your own general knowledge with (general knowledge).\n"
    "- If the web sources and the user's documents disagree, say so explicitly and cite "
    "both.\n"
    "- Note the date when a fact is likely to have changed, and say when sources are "
    "older than the answer requires.\n"
    "- If the sources do not settle the question, say so plainly rather than guessing.\n"
    "- Be concise and concrete. No preamble, do not restate the question, and do not "
    "announce that you are searching."
)

# No documents, no web results: pure model knowledge, clearly labelled.
SYSTEM_PROMPT_MEMORY_ONLY = (
    "You are a helpful assistant. The user's document library and the web search both "
    "returned nothing usable for this question, so answer from your own general "
    "knowledge.\n\n"
    "Rules:\n"
    "- Start your reply with the single line: Not in your documents or search results.\n"
    "- Then answer as helpfully and specifically as you can.\n"
    "- Be honest about the limits of what you know. Do not invent specifics, names, "
    "figures, or dates.\n"
    "- If the question is about something that may have changed recently, say that your "
    "information may be out of date.\n"
    "- Be concise. No preamble."
)


class ChatRequest(BaseModel):
    question: str
    source_id: Optional[str] = None
    visual: bool = False
    top_k: Optional[int] = None
    conversation_id: Optional[str] = None
    # "strict"   answers only from the user's documents
    # "blend"    documents first, then the model's own knowledge, marked inline
    # "research" documents plus a live web search, with separate citations
    mode: str = "strict"


class UrlRequest(BaseModel):
    url: str


_startup_error = ""


@app.on_event("startup")
def _startup() -> None:
    global _startup_error
    db.init_db()
    if ADMIN_BOOTSTRAP["password"]:
        auth.ensure_admin(ADMIN_BOOTSTRAP["username"], ADMIN_BOOTSTRAP["password"])
    elif auth.count_admins() == 0:
        _startup_error = (
            "No admin account exists. Set ADMIN_PASSWORD in .env and restart."
        )
    try:
        store.ensure_collections()
        store.ensure_payload_indexes()
        _startup_error = ""
    except Exception as exc:
        _startup_error = f"{type(exc).__name__}: {exc}"


@app.get("/api/health")
def health() -> dict:
    collections = {"text": False, "images": False}
    error = _startup_error
    try:
        names = [c.name for c in store.get_client().get_collections().collections]
        collections = {
            "text": config.text_collection() in names,
            "images": config.image_collection() in names,
        }
        if not collections["text"]:
            store.ensure_collections()
            collections = {"text": True, "images": True}
        error = ""
    except store.StoreError as exc:
        error = str(exc)
    except Exception as exc:
        error = error or f"{type(exc).__name__}: {exc}"

    configured = bool(config.NVIDIA_API_KEY) and bool(
        config.QDRANT_URL or config.QDRANT_HOST
    )
    return {
        "ok": not error and configured,
        "configured": configured,
        "error": error,
        "models": {
            "embed": config.EMBED_MODEL,
            "clip": config.CLIP_MODEL,
            "ocr": config.OCR_MODEL,
            "chat": config.CHAT_MODEL,
        },
        "collections": collections,
    }


_JOBS: dict = {}


def _make_job(name: str) -> dict:
    job = {
        "job_id": uuid.uuid4().hex,
        "name": name,
        "status": "queued",
        "step": "queued",
        "progress": 0.0,
        "detail": "",
        "result": None,
        "error": "",
        "created_at": time.time(),
    }
    _JOBS[job["job_id"]] = job
    if len(_JOBS) > 200:
        cutoff = time.time() - 7200
        for key in [k for k, v in _JOBS.items() if v["created_at"] < cutoff]:
            _JOBS.pop(key, None)
    return job


def _job_reporter(job: dict):
    def report(step: str, progress: Optional[float] = None, detail: str = "") -> None:
        job["step"] = step
        if progress is not None:
            job["progress"] = max(0.0, min(1.0, float(progress)))
        if detail:
            job["detail"] = detail

    return report


async def _run_ingest(job: dict, factory, on_error=None) -> None:
    """Run an ingest in the background so the request never blocks the browser.

    on_error lets the caller undo any partial state (for uploads, the database
    row and the stored file) so a failed job does not leave a broken entry in
    the library.
    """
    try:
        job["status"] = "running"
        job["step"] = "starting"
        job["progress"] = 0.02
        result = await factory(_job_reporter(job))
        job.update(status="done", step="done", progress=1.0, detail="", result=result)
    except Exception as exc:
        job.update(
            status="error",
            step="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        if on_error:
            try:
                on_error(exc)
            except Exception:
                pass


@app.post("/api/ingest")
async def ingest_file(request: Request, file: UploadFile = File(...)) -> dict:
    user = current_user(request)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded file is empty")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds the upload size limit")

    name = file.filename or "document"
    job = _make_job(name)

    # Register the document and write the original bytes to permanent storage
    # before any parsing, so an upload is never lost to a temp directory.
    record = db.create_source(
        user_id=user["id"],
        name=ingest.safe_name(name),
        kind=extract.classify(name, file.content_type),
        filename=name,
        data=data,
        mime=file.content_type,
        origin="upload",
    )

    async def factory(report):
        async with httpx.AsyncClient() as client:
            result = await ingest.ingest_bytes(
                client, data, name, file.content_type, progress=report,
                user_id=user["id"], source_id=record["source_id"],
            )
        db.finalize_source(
            record["source_id"], result["chunks"], result["images"], result["kind"]
        )
        return result

    asyncio.create_task(_run_ingest(job, factory, on_error=lambda e: _fail_source(record, e)))
    return {"job_id": job["job_id"], "status": job["status"], "source_id": record["source_id"]}


def _fail_source(record: dict, exc: Exception) -> None:
    """Drop the row and file if extraction failed, so the library stays clean."""
    try:
        db.delete_source_row(record["source_id"])
        store.delete_source(record["source_id"])
    except Exception:
        pass


@app.post("/api/ingest/url")
async def ingest_url_endpoint(payload: UrlRequest, request: Request) -> dict:
    user = current_user(request)
    url = payload.url.strip()
    job = _make_job(url)

    async def factory(report):
        async with httpx.AsyncClient() as client:
            result = await ingest.ingest_url(client, url, progress=report, user_id=user["id"])
        # The id must be the one ingest_url used for the Qdrant payloads.
        # Letting create_source mint its own random id left the library row
        # pointing at nothing: no chunk count, no download, orphan vectors.
        db.create_source(
            user_id=user["id"], name=result["source_name"], kind=result["kind"],
            filename=result["source_name"], data=None,
            mime=None, origin="url", source_id=result["source_id"],
        )
        db.finalize_source(
            result["source_id"],
            chunks=result.get("chunks", 0),
            images=result.get("images", 0),
            media_kind=result.get("media_kind") or "text",
        )
        return result

    asyncio.create_task(_run_ingest(job, factory))
    return {"job_id": job["job_id"], "status": job["status"]}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job id")
    return job


@app.get("/api/sources")
def sources(request: Request, owner: Optional[int] = None) -> dict:
    """The caller's library. Admins may pass ?owner=<id> to inspect a user."""
    user = current_user(request)
    is_admin = user["role"] == auth.ROLE_ADMIN
    if owner is not None and not is_admin:
        raise HTTPException(403, "admin access required")
    return {"sources": db.list_sources(user["id"], is_admin, owner_id=owner)}


@app.get("/api/sources/all")
def all_sources(request: Request) -> dict:
    """Master admin view: every user's files in one place."""
    require_admin(request)
    return {"sources": db.list_sources(0, True)}


@app.get("/api/users/{user_id}/sources")
def admin_user_sources(user_id: int, request: Request) -> dict:
    """Master admin view of one user's files."""
    require_admin(request)
    return {"sources": db.list_sources(user_id, True, owner_id=user_id)}


@app.get("/api/sources/{source_id}/file")
def download_source(request: Request, source_id: str) -> FileResponse:
    user = current_user(request)
    try:
        record = db.get_source(source_id)
    except KeyError:
        raise HTTPException(404, "Document not found")
    if user["role"] != auth.ROLE_ADMIN and record["user_id"] != user["id"]:
        raise HTTPException(403, "You do not have access to that document")
    if not record["stored_path"] or not os.path.isfile(record["stored_path"]):
        raise HTTPException(404, "Original file is no longer available")
    return FileResponse(
        record["stored_path"],
        media_type=record["mime"] or "application/octet-stream",
        filename=record["filename"],
    )


@app.get("/api/sources/{source_id}/preview/file")
def preview_inline(request: Request, source_id: str) -> FileResponse:
    """Serve the original with Content-Disposition inline, so <iframe> and the
    browser's PDF viewer can render it (the /file route forces a download)."""
    user = current_user(request)
    try:
        record = db.get_source(source_id)
    except KeyError:
        raise HTTPException(404, "Document not found")
    if user["role"] != auth.ROLE_ADMIN and record["user_id"] != user["id"]:
        raise HTTPException(403, "You do not have access to that document")
    if not record["stored_path"] or not os.path.isfile(record["stored_path"]):
        raise HTTPException(404, "Original file is no longer available")
    return FileResponse(
        record["stored_path"],
        media_type=record["mime"] or "application/octet-stream",
        headers={"Content-Disposition": "inline"},
    )


@app.get("/api/sources/{source_id}/preview")
def preview_source(request: Request, source_id: str) -> dict:
    """Inline-renderable content for the source viewer.

    The /file endpoint forces a download, so an <img> tag renders nothing.
    This returns something a browser can display directly.
    """
    user = current_user(request)
    try:
        record = db.get_source(source_id)
    except KeyError:
        raise HTTPException(404, "Document not found")
    if user["role"] != auth.ROLE_ADMIN and record["user_id"] != user["id"]:
        raise HTTPException(403, "You do not have access to that document")

    name = record["filename"] or record["source_name"] or ""
    suffix = os.path.splitext(name)[1].lower()
    base = {
        "source_id": source_id,
        "source_name": record["source_name"],
        "kind": record["kind"],
        "media_kind": record["media_kind"],
        "filename": name,
        "mime": record["mime"],
        "size_bytes": record["size_bytes"],
    }

    # A page image always wins for PDFs: it is exactly what the user saw.
    page_image = _first_page_image(source_id)
    if suffix == ".pdf":
        if page_image:
            return {
                **base, "preview_type": "pdf",
                "page_image": page_image,
                "download_url": f"/api/sources/{source_id}/file",
                "url": f"/api/sources/{source_id}/preview/file",
            }
        # A text-born PDF has no rendered thumbnail; hand it to the browser's
        # built-in PDF viewer instead.
        if record["stored_path"] and os.path.isfile(record["stored_path"]):
            return {
                **base, "preview_type": "pdf",
                "url": f"/api/sources/{source_id}/preview/file",
                "download_url": f"/api/sources/{source_id}/file",
            }
        return {**base, "preview_type": "unavailable",
                "reason": "The original PDF file is no longer available."}

    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"):
        if page_image:
            return {**base, "preview_type": "image",
                    "url": f"/media/{quote(os.path.basename(page_image))}",
                    "download_url": f"/api/sources/{source_id}/file"}
        # Fall back to the stored original when the page render is gone.
        if record["stored_path"] and os.path.isfile(record["stored_path"]):
            return {**base, "preview_type": "image",
                    "url": f"/api/sources/{source_id}/file",
                    "download_url": f"/api/sources/{source_id}/file"}
        return {**base, "preview_type": "unavailable",
                "reason": "No renderable image for this document."}

    # Text-ish formats: show the extracted text, not the raw bytes.
    text = _extracted_text(source_id)
    if text is not None:
        return {**base, "preview_type": "text", "text": text,
                "download_url": f"/api/sources/{source_id}/file"}

    if record["stored_path"] and os.path.isfile(record["stored_path"]):
        return {**base, "preview_type": "download",
                "download_url": f"/api/sources/{source_id}/file"}
    return {**base, "preview_type": "unavailable",
            "reason": "The original file is no longer available."}


def _first_page_image(source_id: str) -> Optional[str]:
    """Lowest-ordinal page image for a source, if any."""
    try:
        client = store.get_client()
        points, _ = client.scroll(
            collection_name=config.image_collection(),
            scroll_filter=models.Filter(must=[
                models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id))
            ]),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
    except Exception:
        return None
    for point in points:
        path = (point.payload or {}).get("media_path")
        if path and os.path.isfile(config.MEDIA_DIR / os.path.basename(path)):
            return os.path.basename(path)
    return None


def _extracted_text(source_id: str, limit: int = 40000) -> Optional[str]:
    """Reassemble the indexed text for a source so the viewer can show it."""
    try:
        client = store.get_client()
        points, _ = client.scroll(
            collection_name=config.text_collection(),
            scroll_filter=models.Filter(must=[
                models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id))
            ]),
            limit=200,
            with_payload=True,
            with_vectors=False,
        )
    except Exception:
        return None
    chunks = sorted(
        ((p.payload or {}).get("ordinal", 0), (p.payload or {}).get("text") or "")
        for p in points
    )
    text = "\n\n".join(t for _, t in chunks if t.strip())
    return text[:limit] if text.strip() else None


@app.delete("/api/sources/{source_id}")
def remove_source(source_id: str, request: Request) -> dict:
    user = current_user(request)
    try:
        record = db.get_source(source_id)
    except KeyError:
        raise HTTPException(404, "Document not found")
    if user["role"] != auth.ROLE_ADMIN and record["user_id"] != user["id"]:
        raise HTTPException(403, "You do not have access to that document")
    removed = store.delete_source(source_id)
    db.delete_source_row(source_id)
    return {"removed": removed}


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    user = current_user(request)
    scope = None if user["role"] == auth.ROLE_ADMIN else user["id"]
    question = payload.question.strip()
    if not question:
        raise HTTPException(400, "Question is empty")
    top_k = payload.top_k or config.TOP_K
    if payload.mode not in {"strict", "blend", "research"}:
        raise HTTPException(400, "mode must be strict, blend or research")

    async def event_stream():
        started = time.time()
        hits: List[dict] = []
        answer: List[str] = []
        conversation = db.ensure_conversation(user["id"], payload.conversation_id)
        # Save the question up front so it is never lost, even if the
        # stream fails part-way through.
        db.add_message(conversation["id"], user["id"], "user", question, [])
        try:
            async with httpx.AsyncClient() as client:
                if payload.visual:
                    vector = (await nvidia_client.embed_image_queries(client, [question]))[0]
                    hits = store.search_images(vector, top_k, payload.source_id, scope)
                else:
                    vector = (await nvidia_client.embed_texts(client, [question], "query"))[0]
                    hits = store.search_text(vector, top_k, payload.source_id, scope)

                yield _sse("sources", {"hits": hits, "visual": payload.visual})

                want_web = payload.mode == "research" and not payload.visual
                blend = payload.mode in ("blend", "research") and not payload.visual

                # Research mode: gather web sources alongside the user's own
                # documents. This is what lets a question about something the
                # CV merely mentions be answered properly.
                web_sources: List[dict] = []
                if want_web:
                    try:
                        web_sources = await asyncio.wait_for(
                            research.search(client, question, limit=4), timeout=60.0
                        )
                    except Exception:
                        web_sources = []
                    yield _sse(
                        "web",
                        {
                            "count": len(web_sources),
                            "sources": [
                                {"title": s["title"], "url": s["url"]} for s in web_sources
                            ],
                        },
                    )

                if not hits and not web_sources and blend:
                    # Nothing retrieved, but the user asked for the model's own
                    # knowledge, so let it answer instead of refusing.
                    yield _sse("mode", {"mode": "blend", "grounded": False})
                    blend_answer: List[str] = []
                    async for delta in nvidia_client.stream_chat(
                        client,
                        [
                            {"role": "system", "content": SYSTEM_PROMPT_NO_CONTEXT},
                            {"role": "user", "content": question},
                        ],
                    ):
                        blend_answer.append(delta)
                        yield _sse("delta", {"text": delta})

                    text_out = "".join(blend_answer).strip()
                    if not text_out:
                        text_out = (
                            "I could not find anything in the library or online, and did "
                            "not have a reliable answer from memory either."
                        )
                        yield _sse("delta", {"text": text_out})
                    db.add_message(conversation["id"], user["id"], "assistant", text_out, [])
                    yield _sse(
                        "done",
                        {
                            "elapsed": round(time.time() - started, 2),
                            "conversation_id": conversation["id"],
                            "mode": payload.mode,
                            "grounded": False,
                        },
                    )
                    return

                if not hits:
                    no_match = (
                        "I could not find anything in the library that matches that. "
                        "Try different wording, add the document it should be in, or "
                        "switch the answer source above to \u201cResearch\u201d to look "
                        "it up online."
                    )
                    yield _sse("delta", {"text": no_match})
                    # Persist the miss too. Returning without it left the user's
                    # question in the transcript with no reply, so replaying the
                    # chat showed a dangling question.
                    db.add_message(conversation["id"], user["id"], "assistant", no_match, [])
                    yield _sse(
                        "done",
                        {
                            "elapsed": round(time.time() - started, 2),
                            "conversation_id": conversation["id"],
                        },
                    )
                    return

                context = nvidia_client.build_context(hits, payload.visual)
                if payload.visual:
                    instruction = (
                        "The context entries describe images retrieved by visual similarity. "
                        "Describe what they most likely show and how it relates to the question. "
                        "Do not invent detail the descriptions do not support.\n\n"
                    )
                else:
                    instruction = ""

                if want_web and web_sources:
                    system_prompt = SYSTEM_PROMPT_RESEARCH
                elif blend:
                    system_prompt = SYSTEM_PROMPT_BLEND
                else:
                    system_prompt = SYSTEM_PROMPT

                yield _sse(
                    "mode",
                    {
                        "mode": payload.mode,
                        "grounded": bool(hits),
                        "web": len(web_sources),
                    },
                )
                if want_web and web_sources:
                    web_context = research.build_web_context(web_sources)
                    blocks = []
                    if context:
                        blocks.append(f"Passages from the user's documents:\n\n{context}")
                    if web_context:
                        blocks.append(
                            "Passages retrieved from the public web:\n\n" + web_context
                        )
                    user_content = "\n\n".join(blocks) + f"\n\nQuestion: {question}"
                else:
                    user_content = f"{instruction}Context passages:\n\n{context}\n\n" \
                        f"Question: {question}"

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ]

                answered = False
                async for delta in nvidia_client.stream_chat(client, messages):
                    if delta.startswith("__thinking__"):
                        yield _sse("thinking", {"text": delta[len("__thinking__"):]})
                    else:
                        answered = True
                        answer.append(delta)
                        yield _sse("delta", {"text": delta})

                if not answered:
                    yield _sse(
                        "delta",
                        {
                            "text": "I could not produce an answer for that. "
                            "Try rephrasing, or check the document was indexed."
                        },
                    )
                    answer.append(
                        "I could not produce an answer for that. "
                        "Try rephrasing, or check the document was indexed."
                    )

                db.add_message(
                    conversation["id"], user["id"], "assistant", "".join(answer), hits
                )
                yield _sse(
                    "done",
                    {
                        "elapsed": round(time.time() - started, 2),
                        "conversation_id": conversation["id"],
                        "mode": payload.mode,
                        "grounded": bool(hits),
                        "web": len(web_sources),
                    },
                )
        except (nvidia_client.NvidiaError, store.StoreError) as exc:
            yield _sse("error", {"message": str(exc)})
        except Exception as exc:
            yield _sse("error", {"message": f"Chat failed: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/logout")
def api_logout(request: Request) -> dict:
    # A cookie is only a hint; invalidate it server-side too, otherwise the
    # value stays usable until it expires on its own.
    user = getattr(request.state, "user", None)
    if user:
        auth.bump_session_epoch(user["id"])
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/api/me")
def api_me(request: Request) -> dict:
    user = current_user(request)
    return {"user": user, "admins": auth.count_admins()}


@app.post("/api/password")
async def api_change_password(request: Request) -> dict:
    """Change your own password. Requires the current one."""
    user = current_user(request)
    body = await _read_json(request)
    current = body.get("current_password") or ""
    new = body.get("new_password") or ""

    if not auth.check_password(user["username"], current):
        raise HTTPException(401, "Current password is incorrect")
    try:
        auth.set_password(user["id"], new)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


# ------------------------------------------------------------------ admin routes


@app.get("/api/users")
def api_users(request: Request) -> dict:
    require_admin(request)
    return {"users": auth.list_users()}


@app.post("/api/users")
async def api_create_user(request: Request) -> dict:
    """Admin-only user creation. Accounts are never self-registered."""
    require_admin(request)
    body = await _read_json(request)
    try:
        user = auth.create_user(
            (body.get("username") or "").strip(),
            body.get("password") or "",
            body.get("role") or auth.ROLE_USER,
        )
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user": user}


@app.put("/api/users/{user_id}/password")
async def api_set_user_password(user_id: int, request: Request) -> dict:
    require_admin(request)
    body = await _read_json(request)
    try:
        auth.set_password(user_id, body.get("new_password") or "")
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.put("/api/users/{user_id}/active")
async def api_set_user_active(user_id: int, request: Request) -> dict:
    me = require_admin(request)
    body = await _read_json(request)
    try:
        if body.get("is_active") is False and user_id == me["id"]:
            raise auth.AuthError("You cannot disable your own account")
        return {"user": auth.set_active(user_id, bool(body.get("is_active")))}
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/users/{user_id}")
def api_delete_user(user_id: int, request: Request) -> dict:
    me = require_admin(request)
    if user_id == me["id"]:
        raise HTTPException(400, "You cannot delete your own account")
    try:
        auth.delete_user(user_id, protect=me["id"])
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(
        config.FRONTEND_DIR / "login.html", headers={"Cache-Control": "no-store"}
    )


@app.get("/login.html")
def login_page_html() -> FileResponse:
    return login_page()


@app.get("/login.css")
def login_css() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "login.css", media_type="text/css")


@app.get("/login.js")
def login_js() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "login.js", media_type="application/javascript")



@app.get("/api/conversations")
def api_conversations(request: Request) -> dict:
    user = current_user(request)
    return {"conversations": db.list_conversations(user["id"])}


@app.post("/api/conversations")
def api_new_conversation(request: Request) -> dict:
    user = current_user(request)
    return {"conversation": db.new_conversation(user["id"])}


@app.get("/api/conversations/{conversation_id}/messages")
def api_messages(conversation_id: str, request: Request) -> dict:
    user = current_user(request)
    try:
        conv = db.get_conversation(conversation_id)
    except KeyError:
        raise HTTPException(404, "Conversation not found")
    if conv["user_id"] != user["id"] and user["role"] != auth.ROLE_ADMIN:
        raise HTTPException(403, "You do not have access to that conversation")
    return {"messages": db.list_messages(conversation_id, user["id"])}


@app.delete("/api/conversations/{conversation_id}")
def api_delete_conversation(conversation_id: str, request: Request) -> dict:
    user = current_user(request)
    try:
        db.delete_conversation(conversation_id, user["id"])
    except KeyError:
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


@app.get("/api/usage")
def api_usage(request: Request) -> dict:
    user = current_user(request)
    return db.usage_stats(user["id"], user["role"] == auth.ROLE_ADMIN)


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(
        config.FRONTEND_DIR / "admin.html", headers={"Cache-Control": "no-store"}
    )


@app.get("/admin.js")
def admin_js() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "admin.js", media_type="application/javascript")


@app.get("/session.js")
def session_js() -> FileResponse:
    return FileResponse(
        config.FRONTEND_DIR / "session.js", media_type="application/javascript"
    )


@app.get("/media/{name}")
def media(name: str):
    path = (config.MEDIA_DIR / name).resolve()
    if not str(path).startswith(str(config.MEDIA_DIR.resolve())) or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path)


if config.FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
