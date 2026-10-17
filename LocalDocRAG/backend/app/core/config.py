import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def flag(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes"}


@dataclass
class Settings:
    data: Path = Path(os.getenv("DATA_DIR", "/data"))
    ollama_url: str = os.getenv("OLLAMA_URL", "http://ollama:11434")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://qdrant:6333")
    embed_model: str = os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:0.6b")
    vision_model: str = os.getenv("OLLAMA_VISION_MODEL", "qwen3-vl:2b")
    chat_model: str = os.getenv("OLLAMA_CHAT_MODEL", "qwen3:4b")
    mode: str = os.getenv("GPU_MODE", "auto")
    num_ctx: int = int(os.getenv("OLLAMA_CONTEXT_LENGTH", "4096"))
    max_upload: int = int(os.getenv("MAX_UPLOAD_MB", "100")) * 1024 * 1024
    max_expanded: int = 512 * 1024 * 1024
    max_entries: int = 10000
    max_images: int = int(os.getenv("MAX_IMAGE_COUNT_PER_DOC", "300"))
    max_image_side: int = int(os.getenv("MAX_IMAGE_SIDE", "1400"))
    max_pixels: int = 40_000_000
    timeout: int = int(os.getenv("MODEL_CALL_TIMEOUT_SECONDS", "180"))
    batch_size: int = int(os.getenv("INDEX_BATCH_SIZE", "8"))
    dense_k: int = int(os.getenv("RETRIEVE_DENSE_K", "30"))
    bm25_k: int = int(os.getenv("RETRIEVE_BM25_K", "30"))
    final_k: int = int(os.getenv("RAG_FINAL_TOP_K", "8"))
    query_template: str = "Instruct: Given a question, retrieve relevant document passages that answer it\nQuery: {query}"
    caption: bool = flag("ENABLE_IMAGE_CAPTION")
    ocr: bool = flag("ENABLE_OCR")
    docling: bool = flag("ENABLE_DOCLING", False)
    worker: bool = flag("ENABLE_WORKER")
    instance: str = os.getenv("INSTANCE_ID", "local")

    def __post_init__(self):
        self.data = self.data.resolve()
        for url, allowed in [(self.ollama_url, {"ollama", "127.0.0.1", "localhost", "::1"}),
                             (self.qdrant_url, {"qdrant", "127.0.0.1", "localhost", "::1"})]:
            p = urlparse(url)
            if p.scheme != "http" or p.hostname not in allowed or p.username or p.password or p.query or p.path not in {"", "/"}:
                raise ValueError("Only local/internal Ollama and Qdrant endpoints are permitted")
        if not 4096 <= self.num_ctx <= 8192:
            raise ValueError("OLLAMA_CONTEXT_LENGTH must be 4096..8192")
        if self.mode not in {"auto", "cpu", "gpu"}:
            raise ValueError("GPU_MODE must be auto/cpu/gpu")
        self.batch_size = min(16, max(1, self.batch_size))
        for folder in [self.data, self.data / "versions", self.data / "media", self.data / "tmp"]:
            folder.mkdir(parents=True, exist_ok=True)

    @property
    def database(self) -> Path:
        return self.data / "localdoc.sqlite3"

