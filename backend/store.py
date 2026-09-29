import threading
import uuid
from typing import List, Optional, Sequence

from qdrant_client import QdrantClient, models

import config


class StoreError(RuntimeError):
    pass


_client: Optional[QdrantClient] = None
_lock = threading.Lock()


def get_client() -> QdrantClient:
    global _client
    with _lock:
        if _client is not None:
            return _client
        if config.QDRANT_URL:
            url = config.QDRANT_URL
        elif config.QDRANT_HOST:
            url = f"http://{config.QDRANT_HOST}:{config.QDRANT_PORT}"
        else:
            raise StoreError(
                "Set QDRANT_URL (e.g. https://host:6333) or QDRANT_HOST in backend/.env"
            )
        kwargs = {"timeout": config.QDRANT_TIMEOUT, "prefer_grpc": config.QDRANT_PREFER_GRPC}
        if config.QDRANT_API_KEY:
            kwargs["api_key"] = config.QDRANT_API_KEY
        _client = QdrantClient(url=url, **kwargs)
        return _client


def ensure_collections() -> None:
    client = get_client()
    specs = [
        (config.text_collection(), config.TEXT_DIM),
        (config.image_collection(), config.CLIP_DIM),
    ]
    for name, dim in specs:
        if not client.collection_exists(name):
            client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(
                    size=dim, distance=models.Distance.COSINE
                ),
            )
        for field in ("source_id", "source_name", "media_kind"):
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )


def ensure_payload_indexes() -> None:
    """Public entry point for startup."""
    _INDEXED.clear()
    _ensure_payload_indexes()


def reset_collections() -> None:
    client = get_client()
    for name in (config.text_collection(), config.image_collection()):
        if client.collection_exists(name):
            client.delete_collection(name)
    ensure_collections()


def new_source_id() -> str:
    return uuid.uuid4().hex


def upsert_text_chunks(source_id: str, source_name: str, chunks: Sequence[str],
                     vectors: Sequence[Sequence[float]], user_id: int = 0) -> int:
    if not chunks:
        return 0
    points = [
        models.PointStruct(
            id=uuid.uuid4().int & ((1 << 63) - 1),
            vector=list(vector),
            payload={
                "source_id": source_id,
                "source_name": source_name,
                "user_id": int(user_id),
                "media_kind": "text",
                "ordinal": i,
                "text": chunk,
            },
        )
        for i, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]
    client = get_client()
    for i in range(0, len(points), 64):
        client.upsert(
            collection_name=config.text_collection(), points=points[i : i + 64], wait=False
        )
    return len(points)


def upsert_images(source_id: str, source_name: str, media_kind: str,
                   records: Sequence[dict], vectors: Sequence[Sequence[float]],
                   user_id: int = 0) -> int:
    if not records:
        return 0
    points = [
        models.PointStruct(
            id=uuid.uuid4().int & ((1 << 63) - 1),
            vector=list(vector),
            payload={
                "source_id": source_id,
                "source_name": source_name,
                "user_id": int(user_id),
                "media_kind": media_kind,
                "page": record.get("page"),
                "caption": record.get("caption") or "",
                "text": record.get("text") or "",
                "media_path": record.get("media_path"),
            },
        )
        for record, vector in zip(records, vectors)
    ]
    client = get_client()
    for i in range(0, len(points), 64):
        client.upsert(
            collection_name=config.image_collection(), points=points[i : i + 64], wait=False
        )
    return len(points)


USER_ID_KEY = "user_id"
SOURCE_ID_KEY = "source_id"


_INDEXED: set = set()


def _ensure_payload_indexes() -> None:
    """Qdrant refuses a filtered query unless the payload key is indexed.

    Done once per process per collection; Qdrant raises if an index already
    exists, so an existing one is treated as success.
    """
    client = get_client()
    wanted = {
        USER_ID_KEY: models.PayloadSchemaType.INTEGER,
        SOURCE_ID_KEY: models.PayloadSchemaType.KEYWORD,
    }
    for name in (config.text_collection(), config.image_collection()):
        if name in _INDEXED or not client.collection_exists(name):
            continue
        for key, schema in wanted.items():
            try:
                client.create_payload_index(
                    collection_name=name, field_name=key, field_schema=schema, wait=True
                )
            except Exception:
                pass
        _INDEXED.add(name)


def _scope(source_id: Optional[str], user_id: Optional[int]) -> Optional[models.Filter]:
    """Restrict a query to one document and/or one owner.

    user_id=None means "no ownership restriction", which is what an admin
    search does; a normal user always passes their own id.
    """
    if user_id is not None:
        _ensure_payload_indexes()
    must: List = []
    if source_id:
        must.append(models.FieldCondition(key="source_id", match=models.MatchValue(value=source_id)))
    if user_id is not None:
        must.append(models.FieldCondition(key="user_id", match=models.MatchValue(value=int(user_id))))
    return models.Filter(must=must) if must else None


def search_text(vector: Sequence[float], top_k: int, source_id: Optional[str] = None,
                user_id: Optional[int] = None) -> List[dict]:
    query_filter = _scope(source_id, user_id)
    result = get_client().query_points(
        collection_name=config.text_collection(),
        query=list(vector),
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    return [_shape(p.payload or {}, p.score) for p in result.points]


def search_images(vector: Sequence[float], top_k: int, source_id: Optional[str] = None,
                   user_id: Optional[int] = None) -> List[dict]:
    query_filter = _scope(source_id, user_id)
    result = get_client().query_points(
        collection_name=config.image_collection(),
        query=list(vector),
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    return [_shape(p.payload or {}, p.score) for p in result.points]


def _shape(payload: dict, score: float) -> dict:
    return {
        "score": float(score),
        "source_id": payload.get("source_id"),
        "source_name": payload.get("source_name"),
        "media_kind": payload.get("media_kind"),
        "page": payload.get("page"),
        "caption": payload.get("caption"),
        "text": payload.get("text") or "",
        "media_path": payload.get("media_path"),
    }


def list_sources() -> List[dict]:
    client = get_client()
    out = {}
    for name, kind in (
        (config.text_collection(), "text"),
        (config.image_collection(), "images"),
    ):
        if not client.collection_exists(name):
            continue
        records, _ = client.scroll(
            collection_name=name,
            limit=10000,
            with_payload=True,
            with_vectors=False,
        )
        for record in records:
            payload = record.payload or {}
            source_id = payload.get("source_id")
            if not source_id:
                continue
            entry = out.setdefault(
                source_id,
                {
                    "source_id": source_id,
                    "source_name": payload.get("source_name") or "unknown",
                    "media_kind": payload.get("media_kind") or kind,
                    "chunks": 0,
                    "images": 0,
                },
            )
            if kind == "text":
                entry["chunks"] += 1
            else:
                entry["images"] += 1
    return sorted(out.values(), key=lambda s: s["source_name"])


def _deleted_count(response) -> int:
    """Read the deleted-point count from a Qdrant delete response.

    qdrant-client >= 1.7 returns an UpdateResult rather than a list, so the
    count has to come off the result attribute instead of len().
    """
    result = getattr(response, "result", None)
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        for key in ("deleted", "points", "count", "deleted_points"):
            value = result.get(key)
            if isinstance(value, int):
                return value
        return 0
    try:
        return len(response)
    except TypeError:
        return 0


def delete_source(source_id: str) -> int:
    client = get_client()
    removed = 0
    for name in (config.text_collection(), config.image_collection()):
        if not client.collection_exists(name):
            continue
        selector = models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="source_id", match=models.MatchValue(value=source_id)
                    )
                ]
            )
        )
        removed += _deleted_count(client.delete(collection_name=name, points_selector=selector, wait=True))
    return removed
