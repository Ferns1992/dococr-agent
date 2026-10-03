import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")

EMBED_MODEL = os.getenv("EMBED_MODEL", "nvidia/nemotron-3-embed-1b")
CLIP_MODEL = os.getenv("CLIP_MODEL", "nvidia/nvclip")
CHAT_MODEL = os.getenv("CHAT_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
OCR_MODEL = os.getenv("OCR_MODEL", "meta/llama-3.2-11b-vision-instruct")
OCR_ENABLED = os.getenv("OCR_ENABLED", "true").lower() == "true"
OCR_MAX_PER_DOC = int(os.getenv("OCR_MAX_PER_DOC", "30"))

TEXT_DIM = int(os.getenv("TEXT_DIM", "2048"))
CLIP_DIM = int(os.getenv("CLIP_DIM", "1024"))

QDRANT_URL = os.getenv("QDRANT_URL", "")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "")
QDRANT_HOST = os.getenv("QDRANT_HOST", "")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_GRPC_PORT = int(os.getenv("QDRANT_GRPC_PORT", "6334"))
QDRANT_PREFER_GRPC = os.getenv("QDRANT_PREFER_GRPC", "false").lower() == "true"
QDRANT_TIMEOUT = int(os.getenv("QDRANT_TIMEOUT", "30"))
COLLECTION_PREFIX = os.getenv("COLLECTION_PREFIX", "docchat")

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", str(BASE_DIR / "data" / "media")))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1400"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "16"))
CLIP_BATCH = int(os.getenv("CLIP_BATCH", "8"))
TOP_K = int(os.getenv("TOP_K", "6"))
# How many prior turns to feed the model so follow-up questions keep context.
# 0 disables chat memory; 6 keeps the last 6 user/assistant pairs.
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "20"))
HISTORY_WINDOW_MINUTES = float(os.getenv("HISTORY_WINDOW_MINUTES", "60"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

MAX_IMAGE_BYTES = 180 * 1024
IMAGE_MAX_EDGE = 1024

FRONTEND_DIR = BASE_DIR.parent / "frontend"


def text_collection() -> str:
    return f"{COLLECTION_PREFIX}_text"


def image_collection() -> str:
    return f"{COLLECTION_PREFIX}_images"

# Free-tier vision calls hit a 16/16 worker cap; be patient.
OCR_ATTEMPTS = int(os.getenv("OCR_ATTEMPTS", "10"))
