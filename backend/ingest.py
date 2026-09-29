import base64
import logging
import mimetypes
import re
import uuid
from typing import List, Optional, Tuple

import httpx

import config
import extract
import nvidia_client
import store


async def _batched(items: List, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _embed_chunks(client: httpx.AsyncClient, chunks: List[str]) -> List[List[float]]:
    vectors: List[List[float]] = []
    async for batch in _batched(chunks, config.EMBED_BATCH):
        vectors.extend(await nvidia_client.embed_texts(client, batch, "passage"))
    return vectors


async def _embed_images(client: httpx.AsyncClient, images: List[dict]) -> List[List[float]]:
    data_urls: List[str] = []
    for image in images:
        path = config.MEDIA_DIR / image["media_path"]
        payload = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(payload).decode()
        data_urls.append(f"data:{mime};base64,{encoded}")
    vectors: List[List[float]] = []
    async for batch in _batched(data_urls, config.CLIP_BATCH):
        vectors.extend(await nvidia_client.embed_clip(client, batch))
    return vectors


async def _run_ocr(
    client: httpx.AsyncClient, targets: List[dict], progress=None
) -> List[str]:
    if not config.OCR_ENABLED or not targets:
        return []
    selected = targets[: config.OCR_MAX_PER_DOC]
    total = max(1, len(selected))
    blocks: List[str] = []
    for index, target in enumerate(selected, start=1):
        if progress:
            progress(
                "ocr",
                min(0.85, 0.1 + 0.75 * (index - 1) / total),
                f"OCR page {index}/{total}",
            )
        path = config.MEDIA_DIR / target["media_path"]
        if not path.is_file():
            continue
        try:
            text = await nvidia_client.ocr_image(client, path)
        except (nvidia_client.NvidiaError, httpx.HTTPError) as exc:
            logging.getLogger("ingest").warning("OCR failed for %s: %s", path.name, exc)
            continue
        if not text or not text.strip():
            continue
        where = f"page {target['page']}" if target.get("page") else "image"
        blocks.append(f"[OCR — {where}]\n{text}")
    return extract.chunk_text("\n\n".join(blocks))


async def ingest_bytes(
    client: httpx.AsyncClient,
    data: bytes,
    filename: str,
    content_type: Optional[str] = None,
    progress=None,
    user_id: int = 0,
    source_id: Optional[str] = None,
) -> dict:
    if progress:
        progress("extract", 0.05, "extracting text")
    kind = extract.classify(filename, content_type)
    source_id = source_id or store.new_source_id()
    source_name = safe_name(filename)

    if kind == "pdf":
        result = extract.extract_pdf(data, source_name, source_id)
    elif kind == "image":
        result = extract.extract_image(data, source_name, source_id)
    else:
        result = extract.extract_document(data, filename, content_type)

    chunks: List[str] = result["chunks"]
    images: List[dict] = result["images"]
    ocr_chunks: List[str] = []

    if result.get("ocr_targets"):
        ocr_chunks = await _run_ocr(client, result["ocr_targets"], progress=progress)

    if not chunks and not images and not ocr_chunks:
        raise extract.ExtractError(
            f"Nothing indexable found in {source_name}."
        )

    stored_chunks = 0
    stored_images = 0
    embeddable = chunks + ocr_chunks
    if embeddable:
        if progress:
            progress("embed", 0.88, f"embedding {len(embeddable)} passages")
        vectors = await _embed_chunks(client, embeddable)
        stored_chunks = store.upsert_text_chunks(source_id, source_name, embeddable, vectors, user_id)
    if images:
        if progress:
            progress("embed", 0.94, f"embedding {len(images)} images")
        vectors = await _embed_images(client, images)
        stored_images = store.upsert_images(source_id, source_name, kind, images, vectors, user_id)

    if progress:
        progress("store", 0.99, "saved to Qdrant")

    return {
        "source_id": source_id,
        "source_name": source_name,
        "user_id": user_id,
        "kind": kind,
        "chunks": stored_chunks,
        "images": stored_images,
        "ocr_chunks": len(ocr_chunks),
        "pages": result.get("pages"),
    }


async def ingest_url(client: httpx.AsyncClient, url: str, progress=None, user_id: int = 0,
                    source_id: Optional[str] = None) -> dict:
    if not re.match(r"^https?://", url, flags=re.I):
        raise extract.ExtractError("URL must start with http:// or https://")
    async with client.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        if response.status_code >= 400:
            raise extract.ExtractError(f"Fetch failed with HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        chunks: List[bytes] = []
        total = 0
        async for block in response.aiter_bytes():
            chunks.append(block)
            total += len(block)
            if total > config.MAX_UPLOAD_BYTES:
                raise extract.ExtractError("Remote file exceeds the upload size limit")
        data = b"".join(chunks)

    path = url.split("?")[0].rstrip("/")
    filename = path.rsplit("/", 1)[-1] or "page.html"
    if not safe_name(filename).strip():
        filename = "page.html"
    if content_type in ("", "application/octet-stream") and extract.classify(filename, content_type) == "text":
        if b"<html" in data[:2048].lower():
            content_type = "text/html"
    return await ingest_bytes(client, data, filename, content_type or None, progress=progress,
                              user_id=user_id, source_id=source_id)


def safe_name(filename: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", filename).strip() or "document"
    return cleaned[:120]
