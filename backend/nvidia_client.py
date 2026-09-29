import asyncio
import base64
import json
import os
import logging
import mimetypes
from pathlib import Path
from typing import AsyncIterator, List, Optional, Sequence

import httpx

import config


log = logging.getLogger("nvidia")
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class NvidiaError(RuntimeError):
    pass


def _require_key() -> str:
    if not config.NVIDIA_API_KEY:
        raise NvidiaError(
            "NVIDIA_API_KEY is not set. Add it to backend/.env "
            "(get one at https://build.nvidia.com/settings)."
        )
    return config.NVIDIA_API_KEY


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_require_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        return response.text[:500]
    detail = body.get("detail") or body.get("error") or body
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail)
    return f"{response.status_code}: {detail}"


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_body: dict,
    timeout: float,
    attempts: int = 6,
) -> httpx.Response:
    """POST with exponential backoff.

    build.nvidia.com answers 503 ResourceExhausted whenever the free-tier
    per-worker concurrency cap is reached. That is transient, so it is retried
    instead of being surfaced as a hard failure.
    """
    delay = 2.0
    response: Optional[httpx.Response] = None
    for attempt in range(1, attempts + 1):
        response = await client.post(
            url, headers=_headers(), json=json_body, timeout=timeout
        )
        if response.status_code not in _RETRY_STATUS:
            return response
        if attempt == attempts:
            break
        wait = delay * (1.6 ** (attempt - 1))
        log.warning(
            "NVIDIA %s (attempt %d/%d), retrying in %.1fs",
            response.status_code, attempt, attempts, wait,
        )
        await asyncio.sleep(min(wait, 30.0))
    return response


async def embed_texts(
    client: httpx.AsyncClient, texts: Sequence[str], input_type: str
) -> List[List[float]]:
    if not texts:
        return []
    payload = {
        "model": config.EMBED_MODEL,
        "input": list(texts),
        "input_type": input_type,
        "encoding_format": "float",
        "truncate": "END",
    }
    response = await _post_with_retry(
            client, f"{config.NVIDIA_BASE_URL}/embeddings",
            json_body=payload,
            timeout=120.0,
        )
    if response.status_code != 200:
        raise NvidiaError(_extract_error(response))
    data = response.json().get("data") or []
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    vectors = [d["embedding"] for d in ordered]
    for vector in vectors:
        if len(vector) != config.TEXT_DIM:
            raise NvidiaError(
                f"Expected {config.TEXT_DIM}-dim text vectors, got {len(vector)}. "
                "Set TEXT_DIM to match your embedding model."
            )
    return vectors


async def embed_clip(
    client: httpx.AsyncClient,
    items: Sequence[str],
    input_type: str = "passage",
    modality: str = "image",
) -> List[List[float]]:
    if not items:
        return []
    payload = {
        "model": config.CLIP_MODEL,
        "input": list(items),
        "input_type": input_type,
        "modality": modality,
        "encoding_format": "float",
    }
    response = await _post_with_retry(
            client, f"{config.NVIDIA_BASE_URL}/embeddings",
            json_body=payload,
            timeout=120.0,
        )
    if response.status_code != 200:
        raise NvidiaError(_extract_error(response))
    data = response.json().get("data") or []
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    vectors = [d["embedding"] for d in ordered]
    for vector in vectors:
        if len(vector) != config.CLIP_DIM:
            raise NvidiaError(
                f"Expected {config.CLIP_DIM}-dim clip vectors, got {len(vector)}. "
                "Set CLIP_DIM to match your model."
            )
    return vectors


OCR_PROMPT = (
    "Transcribe every piece of text visible in this image. "
    "Preserve reading order, line breaks, table structure, and numbers exactly. "
    "Output only the transcribed text, with no commentary, labels or markdown fences. "
    "If the image contains no text, output exactly: NO_TEXT"
)


async def embed_image_queries(
    client: httpx.AsyncClient, queries: Sequence[str]
) -> List[List[float]]:
    """Embed text queries into the same space as stored images."""
    if not queries:
        return []
    return await embed_clip(client, queries, input_type="query", modality="text")


async def ocr_image(client: httpx.AsyncClient, image_path: Path) -> str:
    payload_bytes = image_path.read_bytes()
    encoded = base64.b64encode(payload_bytes).decode()
    mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    body = {
        "model": config.OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OCR_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{encoded}"},
                    },
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.0,
    }
    response = await _post_with_retry(
            client, f"{config.NVIDIA_BASE_URL}/chat/completions",
            json_body=body,
            timeout=180.0,
            attempts=config.OCR_ATTEMPTS,
        )
    if response.status_code != 200:
        raise NvidiaError(_extract_error(response))
    choices = response.json().get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    text = (message.get("content") or "").strip()
    if text in ("NO_TEXT", "NO TEXT") or len(text) < 3:
        return ""
    return text


_CHAT_ATTEMPTS = 3
_CHAT_RETRY_BASE = 1.5


async def stream_chat(
    client: httpx.AsyncClient,
    messages: List[dict],
    temperature: float = 0.2,
) -> AsyncIterator[str]:
    payload = {
        "model": config.CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": True,
        "max_tokens": 2048,
        # Nemotron 3 Ultra emits a long reasoning_content preamble by default.
        # For retrieval QA that reasoning is wasted budget: it can consume the
        # entire token allowance and leave an empty answer, which is what the
        # user saw as "no reply". Reasoning is surfaced separately instead.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    # The hosted endpoint intermittently truncates a stream: it can close after
    # a single chunk that carries neither content nor a finish_reason. Measured
    # at roughly 1 call in 6. Those were being reported to the user as "I could
    # not produce an answer", so an empty stream is retried instead.
    last_error = ""

    for attempt in range(_CHAT_ATTEMPTS):
        produced = False
        if attempt:
            await asyncio.sleep(_CHAT_RETRY_BASE * (2 ** (attempt - 1)))

        async with client.stream(
            "POST",
            f"{config.NVIDIA_BASE_URL}/chat/completions",
            headers=_headers(),
            json=payload,
            timeout=httpx.Timeout(300.0, connect=15.0),
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                detail = f"{response.status_code}: {body[:500].decode(errors='replace')}"
                if response.status_code in _RETRY_STATUS and attempt < _CHAT_ATTEMPTS - 1:
                    last_error = detail
                    continue
                raise NvidiaError(detail)

            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    if produced:
                        return
                    last_error = "stream closed with no content"
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        produced = True
                        yield text
                        continue
                    thought = delta.get("reasoning_content")
                    if thought:
                        produced = True
                        yield f"__thinking__{thought}"

        if produced:
            return

    raise NvidiaError(
        "The model returned an empty response after %d attempts (%s). "
        "This is a transient upstream issue, not a problem with your library."
        % (_CHAT_ATTEMPTS, last_error or "no content")
    )


def build_context(hits: List[dict], visual: bool) -> str:
    blocks = []
    for i, hit in enumerate(hits, start=1):
        if visual:
            location = f"{hit.get('source_name')} — {hit.get('caption') or 'image'}"
        else:
            page = hit.get("page")
            where = hit.get("source_name")
            if page:
                where = f"{where}, page {page}"
            location = where
        body = (hit.get("text") or "").strip()
        blocks.append(f"[{i}] {location}\n{body}")
    return "\n\n".join(blocks)
