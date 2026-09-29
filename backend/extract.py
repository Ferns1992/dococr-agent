import io
import re
import unicodedata
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image
from pypdf import PdfReader

import config

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".rst", ".csv", ".log", ".json", ".yaml", ".yml"}
HTML_EXTENSIONS = {".html", ".htm"}
DOCX_EXTENSIONS = {".docx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS | HTML_EXTENSIONS | DOCX_EXTENSIONS | {".pdf"}


class ExtractError(RuntimeError):
    pass


def chunk_text(text: str, size: int = None, overlap: int = None) -> List[str]:
    size = size or config.CHUNK_CHARS
    overlap = overlap if overlap is not None else config.CHUNK_OVERLAP
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    pieces: List[str] = []
    current: List[str] = []
    length = 0
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if length + len(block) <= size:
            current.append(block)
            length += len(block) + 2
            continue
        if current:
            pieces.append("\n\n".join(current))
        if len(block) <= size:
            current = [block]
            length = len(block)
            continue
        for segment in _split_long(block, size, overlap):
            pieces.append(segment)
        current = []
        length = 0
    if current:
        pieces.append("\n\n".join(current))
    return [p for p in pieces if p.strip()]


def _split_long(block: str, size: int, overlap: int) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", block)
    out: List[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > size:
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:size])
            sentence = sentence[size - overlap :]
        if len(current) + len(sentence) + 1 <= size:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                out.append(current)
            current = sentence
    if current:
        out.append(current)
    return out


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_docx(data: bytes) -> str:
    import zipfile
    from xml.etree import ElementTree

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("word/document.xml")
    except Exception as exc:
        raise ExtractError(f"Could not read .docx: {exc}") from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ElementTree.fromstring(xml)
    paragraphs = []
    for para in root.iter(f"{namespace}p"):
        runs = [node.text or "" for node in para.iter(f"{namespace}t")]
        line = "".join(runs).strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def _normalise_image(raw: bytes) -> bytes:
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise ExtractError(f"Not a readable image: {exc}") from exc
    image = image.convert("RGB")
    edge = max(image.size)
    if edge > config.IMAGE_MAX_EDGE:
        scale = config.IMAGE_MAX_EDGE / edge
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )
    quality = 85
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    payload = buffer.getvalue()
    while len(payload) > config.MAX_IMAGE_BYTES and quality > 40:
        quality -= 10
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        payload = buffer.getvalue()
    return payload


def _store_image(payload: bytes, stem: str) -> str:
    name = f"{stem}-{uuid.uuid4().hex[:8]}.jpg"
    path = config.MEDIA_DIR / name
    path.write_bytes(payload)
    return name


def extract_pdf(data: bytes, source_name: str, source_id: str) -> dict:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractError(f"Could not open PDF: {exc}") from exc
    pages_text: List[str] = []
    images: List[dict] = []
    ocr_targets: List[dict] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if text:
            pages_text.append(text)
        page_images: List[str] = []
        try:
            for embedded in page.images:
                try:
                    payload = _normalise_image(embedded.data)
                except ExtractError:
                    continue
                media_path = _store_image(payload, source_id)
                page_images.append(media_path)
                images.append(
                    {
                        "page": number,
                        "caption": f"{source_name} page {number}",
                        "text": text[:1500],
                        "media_path": media_path,
                    }
                )
        except Exception:
            pass
        if not text and page_images:
            ocr_targets.append({"page": number, "media_path": page_images[0]})
    return {
        "chunks": chunk_text("\n\n".join(pages_text)),
        "images": images,
        "ocr_targets": ocr_targets,
        "pages": len(reader.pages),
    }


def extract_image(data: bytes, source_name: str, source_id: str) -> dict:
    payload = _normalise_image(data)
    media_path = _store_image(payload, source_id)
    return {
        "chunks": [],
        "images": [
            {
                "page": None,
                "caption": source_name,
                "text": "",
                "media_path": media_path,
            }
        ],
        "ocr_targets": [{"page": None, "media_path": media_path}],
        "pages": None,
    }


def _looks_like_html(data: bytes) -> bool:
    head = data[:2048].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head


def extract_document(data: bytes, filename: str, content_type: Optional[str] = None) -> dict:
    suffix = Path(filename).suffix.lower()
    ctype = (content_type or "").split(";")[0].strip().lower()

    # Dispatch on content type as well as suffix. A URL such as
    # https://github.com/org/repo yields a filename with no extension, so the
    # suffix test alone sent real HTML pages through the plain-decode branch
    # and indexed raw markup instead of the visible text.
    if suffix == ".pdf":
        raise ExtractError("PDF must be routed through extract_pdf")
    if suffix in DOCX_EXTENSIONS:
        text = _read_docx(data)
    elif suffix in HTML_EXTENSIONS or ctype in ("text/html", "application/xhtml+xml") or _looks_like_html(data):
        text = _strip_html(data.decode("utf-8", errors="replace"))
    else:
        text = data.decode("utf-8", errors="replace")
    return {"chunks": chunk_text(text), "images": [], "ocr_targets": [], "pages": None}


def classify(filename: str, content_type: Optional[str] = None) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" or (content_type or "").lower() == "application/pdf":
        return "pdf"
    if suffix in IMAGE_EXTENSIONS or (content_type or "").lower().startswith("image/"):
        return "image"
    if suffix in DOCX_EXTENSIONS:
        return "docx"
    if suffix in HTML_EXTENSIONS or "html" in (content_type or "").lower():
        return "html"
    return "text"


def chunk_texts_with_pages(pages: List[Tuple[int, str]]) -> List[str]:
    merged = "\n\n".join(text for _, text in pages)
    return chunk_text(merged)
